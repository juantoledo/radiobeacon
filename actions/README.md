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
[beacon](../beacon/README.md) — the radio transmit layer, a completely
different responsibility from this event-driven action-chaining layer.
`beacon` subscribes to `item.content_ready` — this
package's `content_ready` correlator's own output, below — as one of its
consumers, same as anything else on the broker.)

## Actions

- **ai** (`src/actions/ai.py`) — on an `item.dispatched`-shaped event,
  looks up the item and asks a configured LLM provider (OpenAI, Claude,
  or a self-hosted Ollama — `ACTIONS_AI_PROVIDER`) to summarize it,
  storing the result in `items.summary`. Normally publishes a single
  CloudEvent to its output topic (`item.ai_settled` by default) once it
  has a valid `(source, item_id)` — even on the skip paths (disabled,
  item not found, no content, content already short, bad provider
  config), with a `summarized: false` marker; only `summarized: true`
  carries the actual `summary` text. This is what lets `chunk` (below)
  subscribe to `ai`'s output and always run *after* it, without going
  silent whenever AI has nothing to add — including the default
  `ACTIONS_AI_ENABLED=false` state. The exception is
  `config.ai_on_failure="abort"` (below), which publishes nothing so the
  item stops here. `ACTIONS_AI_MAX_CHARS` is a skip
  threshold on the *input* — content already at or under that length
  isn't sent to the provider at all — not a cap on the output: whatever
  the provider returns is stored/published verbatim, never truncated
  (the prompt itself asks for a short, complete summary instead). What
  happens when there's no real provider summary — AI disabled, no valid
  provider, the provider call fails or returns nothing — is a per-adapter
  choice, `config.ai_on_failure` (the `/adapters` form's "On AI failure"
  dropdown):
  - `continue_with_contents` (default): the benign skips and an empty
    response store the extracted contents verbatim and keep flowing; a
    provider call that *raises* still propagates and doesn't publish, so no
    misleading `action.ai.executed` (or `chunk` run) happens and the item
    stalls until re-armed.
  - `use_title` (seeded for `senapred`): stores the item's `title` on every
    can't-run / failed path, catching a provider exception too, so the item
    stays on air.
  - `abort`: stores nothing, records an `item.ai_aborted` audit event, and
    publishes nothing — `chunk` never runs, the item never airs. For sources
    whose raw extracted contents are meaningless without AI.

  The three provider clients (`_call_openai` / `_call_claude` / `_call_ollama`)
  now live in `adapters.llm` and are shared with the `aiprompt` adapter type
  (see `data-adapters/README.md`); this module imports them under their
  historical names.

- **chunk** (`src/actions/chunk.py`) — subscribes to `ai`'s own output
  (`item.ai_settled` by default), not `item.dispatched` directly, so it
  always runs *after* `ai` has settled for the same dispatch. Chunks
  `items.summary` when `ai` produced one, falling back to
  `extracted_contents` otherwise (the same summary-else-raw pattern
  `beacon`'s own voice resolution uses) into small, word-boundary-safe
  chunks — `ACTIONS_CHUNK_MAX_CHARS` (default 200) is a *ceiling*, not a
  fixed size: dynamically clamped down further at runtime
  (`adapters.ax25.max_frame_content_bytes`) if the current beacon
  callsign/destination/prefix/suffix would otherwise risk an assembled
  AX.25 frame exceeding its ~256-byte limit (see CONTEXT.md). When
  content splits into more than one chunk, each is suffixed with a
  1-based " i/n" part marker (e.g. " 1/3", " 2/3", " 3/3") baked directly
  into the stored/transmitted text, so a listener catching only one
  AX.25 frame out of several knows its place in the sequence — a single
  chunk is left unmarked. Every chunk is durably stored, in order, in
  the `chunks` table (queryable via
  `./query_history.sh chunks <source> <item_id>` from the repo root)
  before a single completion CloudEvent — not one per chunk — is
  published to its configured output topic, for `content_ready` (below)
  to pick up.

New actions are picked up automatically: `discover_actions()`
(`src/actions/__main__.py`) scans this package's submodules for concrete
`Action` subclasses, so adding one just means adding a new submodule — no
registration step. (data-adapters used the same filesystem-scan pattern
once, but has since moved to config-driven adapter instances — see
[data-adapters/README.md](../data-adapters/README.md#adapter-plugin-types) —
so the two packages' discovery mechanisms have diverged.)

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

Now that `chunk` subscribes to `ai`'s own output (above), `chunk` is
structurally guaranteed to complete after `ai` for the same round — so
`content_ready`'s "wait for both" is no longer the primary race-closer
it was designed as, but stays as cheap defense-in-depth (still correct
even if a future change reintroduces parallelism).

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

An action also declares its default MQTT wiring as three class
attributes — `default_subscribe_topic`, `default_output_topic`,
`default_output_event_type` (all `None` on the base). `__main__.py`
resolves the matching `ACTIONS_<NAME>_*` setting as DB row → env var →
this declared default, so the built-in `ai` → `chunk` → `content_ready`
chain works with nothing in `.env` or the settings table; an env var or
DB row still overrides, and an explicit empty `SUBSCRIBE_TOPIC` disables
the action.

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
| `LOG_LEVEL` | `INFO` (`DEBUG` \| `INFO` \| `WARNING` \| `ERROR`; shared by every service, re-read live — `/config → Logging`) |

Per-action (`ACTIONS_<NAME>_*`, `NAME` = the action's own module name
uppercased, e.g. `CHUNK` for `src/actions/chunk.py`):

Each resolves DB row → env var → the value the action class declares in
code (`default_subscribe_topic` / `default_output_topic` /
`default_output_event_type`, see `src/actions/base.py`). The built-in
`ai` and `chunk` actions declare theirs, so the pipeline flows on a fresh
install with none of these set:

| var | required? | notes |
|---|---|---|
| `ACTIONS_<NAME>_SUBSCRIBE_TOPIC` | no* | comma-separated for multiple topics. Falls back to the action's declared `default_subscribe_topic` (`ai` → `item.dispatched`, `chunk` → `item.ai_settled`). *An action that declares **no** default is skipped (warning logged) when unset. Set to an empty string to explicitly disable an action that has a default. |
| `ACTIONS_<NAME>_OUTPUT_TOPIC` | no | falls back to the declared `default_output_topic` (`ai` → `item.ai_settled`, `chunk` → `item.chunked`); an action that declares none and gets no override is terminal |
| `ACTIONS_<NAME>_OUTPUT_EVENT_TYPE` | no | falls back to the declared `default_output_event_type`, else the action's own module name — the MQTT topic and the CloudEvents `type` are separate concerns |

Chunk-specific: `ACTIONS_CHUNK_MAX_CHARS` (default `200` — a ceiling,
dynamically clamped down at runtime once `BEACON_CALLSIGN` is
configured; see `data-adapters/src/adapters/ax25.py`).

AI-specific: `ACTIONS_AI_ENABLED` (default `false`), `ACTIONS_AI_PROVIDER`
(`openai`/`claude`/`ollama`, required once enabled), `ACTIONS_AI_PROMPT`
(optional global template override — a single adapter can override it
further via `config.ai_prompt`, set on the UI's /adapters form; resolution
order is adapter `config.ai_prompt` → `ACTIONS_AI_PROMPT` → built-in
default. The template may reference any of an item's mapped adapter
attributes as a `{placeholder}` — `{source}`, `{item_id}`,
`{extracted_title}`, `{extracted_contents}`, `{summary}`, `{url}`,
`{event_key}`, `{type}`, `{subtype}`, `{transmit_policy}`,
`{source_date_time}`, `{fetched_at}`, `{captured_at}`, `{rawdata}` (see
`actions.ai.PROMPT_ITEM_FIELDS`), plus the source's display name / site
URL as `{source_name}` / `{source_url}` (the same placeholders the beacon
& chunk templates use); an unknown placeholder renders blank),
`ACTIONS_AI_MAX_CHARS` (default `200` —
matches `ACTIONS_CHUNK_MAX_CHARS` exactly, so both share one skip/chunk
length budget; not used for voice length — see `BEACON_VOICE_MAX_CHARS` in
[beacon](../beacon/README.md)), `ACTIONS_AI_<PROVIDER>_MODEL`,
`ACTIONS_AI_OLLAMA_HOST`, `ACTIONS_AI_EVENT_INCLUDE_PROMPT` (default
`false`; when `true`, the fully rendered prompt is attached to each
`item.ai_settled` event), plus the unprefixed
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` read directly by each SDK — see
`.env.example`.

Per-adapter (not env vars — `adapter_instances.config`, set on the
`/adapters` form): `ai_prompt` (above) and `ai_on_failure`. The
latter defaults off — a failed provider call propagates and the item stops
(no publish, no `action.ai.executed` row), and the AI-disabled skip copies
`extracted_contents` into `items.summary`. When on (seeded `true` for
`senapred`), a failed provider call is caught, and when AI is disabled the
skip stores the title too: in both cases the item's `extracted_title`
(falling back to `extracted_contents`) is stored as `items.summary`, and a
normal `summarized: false` event is published so `chunk` still runs.

Every `item.ai_settled` event carries diagnostic detail beyond the
load-bearing `source`/`item_id`/`summarized` fields: a `reason` phrase on
every `summarized: false` path (item missing, AI disabled, content
already short, bad provider config, ...), `provider`/`model` on success,
and the rendered `prompt` when `ACTIONS_AI_EVENT_INCLUDE_PROMPT=true`. All
of these are also mirrored into the `action.ai.executed` audit row, so
they show up on the UI's `/audit` page — `prompt` included, on the same
`ACTIONS_AI_EVENT_INCLUDE_PROMPT` opt-in.

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
