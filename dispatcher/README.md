# dispatcher

Watches `storage/radiobeacon.db` for newly-inserted items and delivers
each one to a set of handlers — the component that turns "a new row
appeared" into "something happened." Fully decoupled from
[data-adapters](../data-adapters/README.md): it only reads `items`, polling on its
own interval — connected to the rest of the pipeline only through the
database.

Right now the only handler is `log_handler`, which just logs. This is the
extension point for real delivery later (radio TX, notifications, etc.) —
see `dispatcher/src/dispatcher/__main__.py`'s `HANDLERS` list.

## How it works

Every poll:
1. **Discover** — any row in `items` with a `rowid` past this consumer's
   watermark is newly seen, and gets armed for delivery.
2. **Sync policy changes** — every row in `items` (not just newly
   discovered ones) has its `transmit_policy` compared against what this
   consumer last recorded for it; a mismatch arms it too — this is what
   makes a `transmit_policy` edit re-flow through the pipeline even for an
   item that's already delivered/retired, or one that predates this
   consumer entirely (see below).
3. **Dispatch** — every armed item is passed to every handler **exactly
   once**, then its tracking row is deleted (success or failure alike).

The dispatcher does not repeat deliveries. "Put this item on air N times,
spaced out" is a *transmit* concern and lives in
[beacon](../beacon/README.md)'s `beacon_tx_schedule`, downstream of content
preparation — see "Transmit policy" below.

**First run skips the backlog**: a brand-new consumer's watermark starts
at the database's current max `rowid`, not `0`, and every item's
`transmit_policy` is silently baselined (not treated as a change) the
first time step 2 sees it — so pointing `dispatcher` at an already-populated
database doesn't immediately fire on every historical row. To
intentionally replay history for a consumer, delete its rows from the
`dispatcher_state` **and** `item_policy_state` tables
(`sqlite3 storage/radiobeacon.db "DELETE FROM dispatcher_state WHERE consumer = '...'; DELETE FROM item_policy_state WHERE consumer = '...'"`)
and restart.

**A brand-new `items` row can still be a stale event**, though: an
adapter's own first poll can return a batch of already-old real-world
events in one response (e.g. CSN/SENAPRED returning the last N
earthquakes/alerts, not just ones from this exact moment) — each lands
as a genuinely new `rowid`, so the watermark above doesn't catch it.
Step 1 (`discover_new_items`) additionally compares each new row's own
`source_date_time` (the event's real-world timestamp — see
[data-adapters/README.md](../data-adapters/README.md)) against
`not_before`, this process's own startup instant (captured once in
`__main__.py`, passed through every poll). Anything whose
`source_date_time` predates it is recorded as seen (so it's never
reconsidered) but never armed for dispatch — filtered at the earliest
possible point, before it ever reaches `trigger_dispatches`, rather than
downstream. Not a rolling max-age: a genuinely new event is never
excluded no matter how long this process keeps running afterward. An
item with no `source_date_time` at all is never treated as stale (there's
nothing to compare).

## Transmit policy (`transmit_policy`)

Every item carries a `transmit_policy` column (a name, set by the adapter
that produced it — see
[data-adapters/README.md](../data-adapters/README.md#adapter-contract)) that's a
soft reference (not a SQL `FOREIGN KEY`) into the `transmit_policies`
table. That table is **owned by `data-adapters`**
([`adapters.storage`](../data-adapters/src/adapters/storage.py) creates and
seeds it, same as `sources`), so every package reaches it through the same
`get_connection()`:

```sql
CREATE TABLE transmit_policies (
    name TEXT PRIMARY KEY,
    repeat_times INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    description TEXT
);
```

Seeded on first run with a single tier:

| name | repeat_times | interval_seconds |
|---|---|---|
| `informational` | 1 | 0 |

Any further tier is added by an operator (see below), not seeded.

**The dispatcher doesn't act on `repeat_times`/`interval_seconds` at all** —
it delivers each item once. Those numbers are read by
[beacon](../beacon/README.md), which schedules `repeat_times` on-air
transmissions of the item spaced `interval_seconds` apart (see
`beacon_tx_schedule` in [beacon/README.md](../beacon/README.md)). An item
with no `transmit_policy`, an unrecognized one, or one that's been deleted
falls back to `informational`; if even that row is gone, a hardcoded
`(1, 0)` is used as an absolute last resort.

### Managing policies

```bash
./policies.sh list
./policies.sh set <name> --repeat-times N --interval-seconds N [--description TEXT]
./policies.sh delete <name>
```

(also editable at the ui's `/policies` page). Adding a new named tier
(e.g. `urgent` at 5x/60s, or `critical` at 10x/30s) is a `set` call, not a
code change — any adapter can then point items at it, or an operator can
point an existing item at it via `override_item.sh` (below). An API adapter
can also assign a tier per item from a numeric threshold via the
`transmit_policy_rule` in its config (e.g. earthquakes at/above a magnitude
threshold → `urgent`) — see
[data-adapters/README.md](../data-adapters/README.md#adapter-plugin-types),
editable at `/adapters`. No seeded adapter sets one by default.

### Manual overrides / rearm

`transmit_policy` is the sanctioned exception to `items` rows being
immutable once stored — like `summary`, a separate actor (the ui, or a
manual write) may update this column after the fact. **Reassigning it**
(via `override_item.sh`, the ui, or any direct write to `items`) is caught
by the "sync policy changes" step every poll, comparing against
`item_policy_state` — a mismatch arms one fresh dispatch, which re-flows
the pipeline so beacon re-schedules the item's transmissions under the new
tier, **regardless of the item's current state** (armed, already retired,
or backlog).

`--rearm` covers the one thing a `transmit_policy` change doesn't:
re-sending an item under the *same* policy it's already on.

```bash
./override_item.sh <source> <item_id> --transmit-policy NAME [--rearm] [--consumer NAME]
```

- `--transmit-policy` updates the item's column to any policy name
  (doesn't need to already exist in `transmit_policies` — same
  soft-reference/fallback behavior as an adapter-set one).
- `--rearm` re-inserts a `trigger_dispatches` row for `--consumer`
  (defaults to `DISPATCHER_CONSUMER_NAME`) — only has an effect if the item
  is currently retired; a no-op (logged) if it's still armed or doesn't
  exist.

`dispatcher.override` also has `reset_dispatch_state(conn, consumer,
source, item_id)` — a debug-only operation (no CLI wrapper; used by
[ui/](../ui/README.md)'s Developers section) that clears a consumer's
`trigger_dispatches`/`item_policy_state` rows for an item *without*
re-arming it, unlike `--rearm` above. After it, the item is neither armed
nor recorded as "seen" by that consumer at all — for clearing stuck or
incorrect bookkeeping while debugging, not a normal delivery control.

### Import / Export config

```bash
./export_config.sh [--db PATH] [-o FILE]     # prints JSON to stdout, or writes FILE
./import_config.sh INPUT [--db PATH] [--dry-run]
```

A whole-DB config snapshot — settings overrides, `transmit_policies`,
source display names, and every `adapter_instances` row (bundled with its
`sources` row) — as one JSON file. Logic lives in
[data-adapters](../data-adapters/README.md)'s `adapters.config_transfer`
(shared with the ui's `/config/import-export` page); this CLI pair lives
here rather than there only because registering the MQ audit-event hook
needs `dispatcher.mq_publisher`, same reason `policies.sh` lives here
instead of next to `adapters/transmit_policy.py`.

Secrets (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) are never exported, not even
as a placeholder key. Import is merge/upsert-only — a record overwrites an
existing one sharing its name/source; nothing already in the target DB
that's absent from the file is ever deleted. `--dry-run` validates and
prints the same per-section created/updated/skipped/warned summary without
writing anything. A CUSTOM-type adapter (operator-authored Python, `exec`'d
with no sandboxing) only imports when the target DB's `UI_DEV_TOOLS_ENABLED`
is on; its code is never executed or test-run during import either way.

## Publishing to a message queue (CloudEvents over MQTT)

Optionally, 6 of the audit events this package records (see "State" below
for the full `audit_log` picture) are also published to a local MQTT
broker as [CloudEvents](https://cloudevents.io) — a subscriber (radio TX,
notifications, another service) can react without polling `audit_log`
itself:

- `item.dispatched` — the successful handler call for an item. One per
  item now (the dispatcher delivers exactly once), so every one is
  published — this is what drives `actions/` and, downstream, beacon.
- `item.dispatch_failed` — every failed handler call.
- `item.discovered` — a new item armed for delivery.
- `item.policy_drifted` — an item's `transmit_policy` changed since last seen.
- `item.policy_overridden`, `item.rearmed` — via `override_item.sh` / the ui.

`adapter.fetch`, `item.stored`, `policy.set`, `policy.deleted` are
recorded in `audit_log` but never published here.

Enabled by default: `DISPATCHER_MQ_HOST` resolves to `localhost` when
unset, the same as `ACTIONS_MQ_HOST` / `BEACON_MQ_HOST` — set it to an
empty string (`DISPATCHER_MQ_HOST=` in `.env`, or a blank settings row) to
disable, in which case no connection is ever attempted. This is also the
only way `actions/` (chunk, ai) learns
about new or re-armed items: it subscribes to the same
`radiobeacon/events/item.dispatched` topic published here, so disabling
this leaves `actions` permanently idle even if it and the broker are
both running. See [mq/README.md](../mq/README.md) for the local
Mosquitto broker this points at by default (`start.sh` starts it
automatically).

Each event is published to the topic `radiobeacon/events/<event_type>`
(e.g. `radiobeacon/events/item.dispatched`) — subscribe to
`radiobeacon/events/#` to receive all of them, or an exact topic for one
event type. Each message payload is a structured-mode CloudEvents JSON
envelope, e.g.:

```json
{
  "specversion": "1.0",
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "source": "radiobeacon/log_handler",
  "type": "cl.radiobeacon.item.dispatched",
  "time": "2026-08-21T14:32:07.123456+00:00",
  "data": {
    "source": "senapred",
    "item_id": "abc123",
    "actor": "log_handler",
    "details": {
      "consumer": "log",
      "transmit_policy": "informational"
    }
  }
}
```

Note the two different "source" fields: the CloudEvents envelope's
top-level `source` (who emitted the event — `radiobeacon/<actor>`) vs.
`data.source` (radiobeacon's own domain concept — which adapter the item
belongs to, e.g. `"senapred"`).

A publish failure (broker down, etc.) is logged and never breaks
delivery — the `audit_log` row is always the source of truth; MQTT
publishing is best-effort on top of it.

## Usage

```bash
./start.sh
```

Same pattern as `data-adapters/start.sh`: creates a `.venv`, installs
`requirements.txt`, loads `../.env`, then `exec`s into a **long-running**
poll loop (`PYTHONPATH=src python3 -m dispatcher`). `Ctrl+C`/`SIGTERM` stops
it cleanly.

## Configuration

Env vars, in `.env` at the repo root (see `.env.example`). Per-policy
repeat/interval config is **not** here — see "Managing policies" above.

| var | default |
|---|---|
| `DISPATCHER_INTERVAL_SECONDS` | `5` |
| `DISPATCHER_CONSUMER_NAME` | `log` |
| `DISPATCHER_MQ_HOST` | `localhost` |
| `DISPATCHER_MQ_PORT` | `1883` |
| `DISPATCHER_MQ_QOS` | `1` |
| `DISPATCHER_MQ_CONNECT_TIMEOUT_SECONDS` | `5` |

## State

Three tables, owned by this package (separate from `items`, which stays
adapter-owned, and `transmit_policies`, owned by `data-adapters`):

- `dispatcher_state (consumer, last_seen_rowid)` — the discovery watermark.
- `trigger_dispatches (consumer, source, item_id)` — the "this item is
  armed for one delivery" marker. Deleted the moment it's delivered.
- `item_policy_state (consumer, source, item_id, transmit_policy)` — the
  `transmit_policy` this consumer last saw for *every* item it's ever
  looked at — the baseline the "sync policy changes" step compares against
  to detect an edit, including on already-retired or backlog items.

## Tests

```bash
.venv/bin/pytest tests/ -v
```
