# actions

Runs N independently-configured "actions" — each subscribes to one or
more MQTT topics on the local broker (see [../mq/README.md](../mq/README.md)),
does some work when a message arrives, and optionally publishes a result
to another topic for a downstream action to consume. A chained pipeline
wired entirely through MQTT topics and env-var configuration — no direct
code coupling between actions, and no code coupling to
[dispatcher](../dispatcher/README.md) either (dispatcher publishes
CloudEvents; this package is one possible subscriber, alongside anything
else that wants to listen).

(Not called "orchestrator": that name belongs to
[beacon](../beacon/README.md) — the TDMA radio transmit-slot timing
scheduler, a completely different responsibility from this event-driven
action-chaining layer. `beacon` subscribes to this package's own output
topics — `item.chunked` for frame content, `item.dispatched` directly for
voice — as one of its consumers, same as anything else on the broker.)

## Actions

- **chunk** (`src/actions/chunk.py`) — on an `item.dispatched`-shaped
  event, looks up the item's `extracted_contents` in `items` and splits
  it into small, word-boundary-safe chunks (`ACTIONS_CHUNK_MAX_CHARS`,
  default 200 — headroom under AX.25's ~256-byte UI frame payload limit,
  see CONTEXT.md), durably storing every chunk, in order, in the `chunks`
  table (queryable via `./query_history.sh chunks <source> <item_id>`
  from the repo root) before publishing a single completion CloudEvent —
  not one per chunk — to its configured output topic, for a future
  downstream action (e.g. an AX.25 formatter) to go query the stored
  chunks and consume.

- **ai** (`src/actions/ai.py`) — on an `item.dispatched`-shaped event,
  looks up the item and asks a configured LLM provider (OpenAI, Claude,
  or a self-hosted Ollama — `ACTIONS_AI_PROVIDER`) to summarize it,
  storing the result in `items.summary` and publishing a single
  `item.summarized` CloudEvent carrying the summary text directly (one
  bounded value, unlike `chunk`'s N rows, so no need for a pointer-only
  event). `ACTIONS_AI_MAX_CHARS` is a skip threshold on the *input* —
  content already at or under that length isn't sent to the provider at
  all — not a cap on the output: whatever the provider returns is
  stored/published verbatim, never truncated (the prompt itself asks for
  a short, complete summary instead). **Disabled by default**
  (`ACTIONS_AI_ENABLED=false`) — unlike `chunk`, this has a real per-call
  cost (a paid API, or a hard dependency on a local Ollama install), so
  it's opt-in. Once enabled, `ACTIONS_AI_PROVIDER` is required with no
  default. A provider call failure is not swallowed — it propagates so no
  misleading `action.ai.executed` audit event is recorded for a message
  that actually failed.

New actions are picked up automatically: `discover_actions()`
(`src/actions/__main__.py`) scans this package's submodules for concrete
`Action` subclasses, so adding one just means adding a new submodule — no
registration step (same pattern as
[data-adapters](../data-adapters/README.md#adapters)' `discover_adapters()`).

## content_ready — not an action, a correlator

`src/actions/content_ready.py` is not an `Action` subclass — it doesn't
react to one message, it watches for the point where **both** `chunk`
and `ai` have finished reacting to the same dispatch (`audit_log` rows
`action.chunk.executed` and `action.ai.executed` both present for a
given `(source, item_id)`), then publishes one `item.content_ready`
CloudEvent — the race-free "everything that was going to happen to this
item's content has happened" signal
[beacon](../beacon/README.md) subscribes to. `action.<name>.executed` is
recorded on every normal `run()` return, including skips (short content,
`ACTIONS_AI_ENABLED=false`) — only an uncaught exception skips it — so
this is a reliable signal regardless of whether either action actually
produced output.

Runs on its own poll loop (`_run_content_ready_loop` in
`src/actions/__main__.py`), not a subscription — polling instead of
`register_audit_event_hook` deliberately, since `chunk` and `ai` each run
on independent MQTT threads and two hooks firing at once could both
observe "both done" and double-publish; a single poll loop serializes
the check instead. A rearm doesn't delete prior audit rows, so "already
published" is a timestamp comparison (`item_readiness.published_at` vs.
the latest `action.*.executed` `recorded_at` — see
`data-adapters/src/adapters/storage.py`), not row existence — a rearm's
fresh completions naturally produce a fresh publish.

The published event's `data` includes `has_summary: bool` (whether
`items.summary` was set at publish time) — beacon's routing signal for
whether to send one AI-summary frame or one frame per raw chunk; actual
transmitted text is still resolved fresh from the DB at transmit time.

## Action contract

Every action subclasses `Action` (`src/actions/base.py`) and implements:

```python
run(self, event: dict, *, conn: sqlite3.Connection) -> list[dict]
```

`event` is the full parsed CloudEvent envelope as a dict (`specversion`,
`type`, `source`, `id`, `time`, `data`, ...) — not just `data` — so an
action subscribed to more than one topic can branch on `event["type"]`.
`conn` is a fresh sqlite3 connection the runner opens for this one
message and closes afterward.

Returns zero or more output payloads — each dict becomes one CloudEvent's
`data` field, published to this action's configured output topic. Return
`[]` for "nothing to publish this time" (a normal outcome) or when no
`OUTPUT_TOPIC` is configured at all (a terminal action — perfectly valid,
not every action needs somewhere downstream to publish to).

A fresh action instance is created per message, so actions stay
stateless between messages by construction.

## How a message is handled

Each action runs on its own thread, with its own MQTT connection (full
failure isolation — one action's crash or reconnect storm never touches
another's). On message:

1. Parse the payload as a CloudEvent (`src/actions/mq.py`).
2. Open a DB connection, call the action's `run()`.
3. Record an `action.<name>.executed` audit event (see
   [dispatcher/README.md](../dispatcher/README.md) for the shared
   `audit_log` this writes to).
4. Publish each returned output as its own CloudEvent to the action's
   configured output topic, if any.

Every step is wrapped in one unconditional `try`/`except` — paho-mqtt
re-raises an uncaught exception from `on_message`, which would otherwise
silently kill that action's background network thread permanently (no
next poll to recover on, unlike a normal interval loop). A failure here
is logged and swallowed; the action keeps listening for the next message.

## Setup

```bash
./start.sh
```

Creates a `.venv`, installs `requirements.txt`, loads `../.env` (if
present), then `exec`s into `PYTHONPATH=src python3 -m actions` — a
**long-running process**, not a one-shot script. `Ctrl+C` (or `SIGTERM`)
stops every action's loop and exits cleanly.

## Configuration

Env vars, in `.env` at the repo root (see `.env.example`).

Package-generic (all actions share one broker connection config):

| var | default |
|---|---|
| `ACTIONS_MQ_HOST` | `localhost` |
| `ACTIONS_MQ_PORT` | `1883` |
| `ACTIONS_MQ_QOS` | `1` |
| `ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS` | `5` |

Per-action (`ACTIONS_<NAME>_*`, `NAME` = the action's own module name
uppercased, e.g. `CHUNK` for `src/actions/chunk.py`):

| var | required? | notes |
|---|---|---|
| `ACTIONS_<NAME>_SUBSCRIBE_TOPIC` | yes | comma-separated for multiple topics; the action is skipped (warning logged) if unset — there's no sensible generic default, unlike e.g. `ADAPTERS_DEFAULT_INTERVAL_SECONDS` |
| `ACTIONS_<NAME>_OUTPUT_TOPIC` | no | omit for a terminal action |
| `ACTIONS_<NAME>_OUTPUT_EVENT_TYPE` | no | defaults to the action's own module name (e.g. `chunk`); set explicitly (e.g. `item.chunked`) for readability — the MQTT topic and the CloudEvents `type` are separate concerns |

Chunk-specific: `ACTIONS_CHUNK_MAX_CHARS` (default `200`).

AI-specific: `ACTIONS_AI_ENABLED` (default `false`), `ACTIONS_AI_PROVIDER`
(`openai`/`claude`/`ollama`, required once enabled), `ACTIONS_AI_PROMPT`
(optional template override), `ACTIONS_AI_MAX_CHARS` (default `200` —
matches `ACTIONS_CHUNK_MAX_CHARS` exactly, so both channels
[beacon](../beacon/README.md) reads from share one length budget),
`ACTIONS_AI_<PROVIDER>_MODEL`, `ACTIONS_AI_OLLAMA_HOST`, plus the
unprefixed `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` read directly by each
SDK — see `.env.example`.

content_ready-specific: `ACTIONS_CONTENT_READY_POLL_INTERVAL_SECONDS`
(default `2`), `ACTIONS_CONTENT_READY_OUTPUT_TOPIC` (default
`radiobeacon/events/item.content_ready`).

## Tests

```bash
.venv/bin/pytest tests/ -v
```

No real broker needed — `paho.mqtt`'s `Client`/`publish` are monkeypatched
at the attribute level, same convention as
[dispatcher/tests/test_mq_publisher.py](../dispatcher/tests/test_mq_publisher.py).
