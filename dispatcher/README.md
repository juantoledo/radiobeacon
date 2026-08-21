# dispatcher

Watches `storage/radiobeacon.db` for newly-inserted items and delivers
each one to a set of handlers — the component that turns "a new row
appeared" into "something happened." Fully decoupled from
[adapters](../adapters/README.md): it only reads `items`, polling on its
own interval — connected to the rest of the pipeline only through the
database.

Right now the only handler is `log_handler`, which just logs. This is the
extension point for real delivery later (radio TX, notifications, etc.) —
see `dispatcher/src/dispatcher/__main__.py`'s `HANDLERS` list.

## How it works

Every poll:
1. **Discover** — any row in `items` with a `rowid` past this consumer's
   watermark is newly seen, and gets scheduled for immediate delivery.
2. **Sync policy changes** — every row in `items` (not just newly
   discovered ones) has its `dispatch_policy` compared against what this
   consumer last recorded for it; a mismatch schedules it for immediate
   delivery too, with a fresh delivery count — this is what makes a
   `dispatch_policy` edit reactive even for an item that's already fully
   delivered/retired, or one that predates this consumer entirely (see
   below).
3. **Dispatch** — every scheduled item that's currently due gets passed to
   every handler, then is rescheduled or retired based on the repeat
   policy its `dispatch_policy` column names (see below).

**First run skips the backlog**: a brand-new consumer's watermark starts
at the database's current max `rowid`, not `0`, and every item's
`dispatch_policy` is silently baselined (not treated as a change) the
first time step 2 sees it — so pointing `dispatcher` at an already-populated
database doesn't immediately fire on every historical row. To
intentionally replay history for a consumer, delete its rows from the
`dispatcher_state` **and** `item_policy_state` tables
(`sqlite3 storage/radiobeacon.db "DELETE FROM dispatcher_state WHERE consumer = '...'; DELETE FROM item_policy_state WHERE consumer = '...'"`)
and restart.

## Delivery repeat policy (`dispatch_policy`)

Every item carries a `dispatch_policy` column (a name, set by the adapter
that produced it — see
[adapters/README.md](../adapters/README.md#adapter-contract)) that's a
soft reference (not a SQL `FOREIGN KEY`) into this package's own
`dispatch_policies` table, which centralizes the actual delivery config:

```sql
CREATE TABLE dispatch_policies (
    name TEXT PRIMARY KEY,
    repeat_times INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    description TEXT
);
```

Seeded on first run with the two policies everything defaults to:

| name | repeat_times | interval_seconds |
|---|---|---|
| `urgent` | 5 | 60 |
| `informational` | 1 | 0 |

An item on the `urgent` policy is delivered immediately, then redelivered
every `interval_seconds` until `repeat_times` is reached — giving a
future radio TX layer several chances (e.g. multiple TDMA cycles) to
actually get it on air — then its tracking row is retired.
`informational` just fires once. An item with no `dispatch_policy`, an
unrecognized one, or one that's been deleted falls back to
`informational`; if even that row is gone, a hardcoded `(1, 0)` is used
as an absolute last resort — delivery can never crash or loop forever
from a management mistake.

### Managing policies

```bash
./policies.sh list
./policies.sh set <name> --repeat-times N --interval-seconds N [--description TEXT]
./policies.sh delete <name>
```

This is the actual point of centralizing the config in a table: adding a
new named policy (e.g. a future `critical` needing 10x/30s) is a `set`
call, not a code change or a new `items` column — any adapter can then
point items at it via its own `dispatch_policy` logic, or an operator can
point an existing item at it via `override_item.sh` (below). CSN is the
current example of the latter kind of per-item nuance: earthquakes
at/above `ADAPTERS_CSN_URGENT_MAGNITUDE_THRESHOLD` get `dispatch_policy =
"urgent"`, below it `"informational"` — two items from the same adapter,
different treatment, no override machinery needed beyond picking a name.

### Manual overrides take effect immediately — for any row, not just in-flight ones

`dispatch_policy` is the sanctioned exception to `items` rows being
immutable once stored — like `summary`, a separate actor may update this
column after the fact (e.g. a future UI, or manually). Two things react
live, never from a value fixed at some earlier point in time:

- **A policy's own `repeat_times`/`interval_seconds` change** (e.g. via
  `policies.sh`) takes effect for every item currently on that policy on
  the very next poll: `dispatch_due_items` computes due-ness fresh every
  call, so shortening `interval_seconds` makes a pending item due sooner
  immediately, lengthening it pushes the item out immediately — no
  waiting for the old interval to elapse first.
- **Reassigning an item's `dispatch_policy` itself** (e.g. via
  `override_item.sh`, or any direct write to `items`) is caught by the
  "sync policy changes" step every poll, comparing against
  `item_policy_state` (what this consumer last recorded for that item) —
  a mismatch schedules a fresh dispatch right now, with a full delivery
  budget under the new policy, **regardless of the item's current
  state**: still in-flight, already fully retired, or never even
  discovered as "new" in the first place (predates this consumer's
  watermark). No `--rearm` needed for this case — see the two tests
  named for it in `dispatcher/tests/test_watcher.py` if you want the exact
  mechanics.

`--rearm` still exists for the one thing a `dispatch_policy` change
doesn't cover: redelivering an item under the *same* policy it's already
on (e.g. "resend this exact alert again").

```bash
./override_item.sh <source> <item_id> --dispatch-policy NAME [--rearm] [--consumer NAME]
```

- `--dispatch-policy` updates the item's column to any policy name
  (doesn't need to already exist in `dispatch_policies` — same
  soft-reference/fallback behavior as an adapter-set one).
- `--rearm` re-inserts a due `trigger_dispatches` row for `--consumer`
  (defaults to `DISPATCHER_CONSUMER_NAME`) — only has an effect if the item
  is currently retired; a no-op (logged) if it's still in-flight or
  doesn't exist.

## Usage

```bash
./start.sh
```

Same pattern as `adapters/start.sh`: creates a `.venv`, installs
`requirements.txt`, loads `../.env`, then `exec`s into a **long-running**
poll loop (`PYTHONPATH=src python3 -m dispatcher`). `Ctrl+C`/`SIGTERM` stops
it cleanly.

## Configuration

Env vars, in `.env` at the repo root (see `.env.example`). Delivery repeat
config (`repeat_times`/`interval_seconds` per policy) is **not** here —
see "Managing policies" above.

| var | default |
|---|---|
| `DISPATCHER_INTERVAL_SECONDS` | `5` |
| `DISPATCHER_CONSUMER_NAME` | `log` |

## State

Four tables, owned by this package (separate from `items`, which stays
adapter-owned):

- `dispatcher_state (consumer, last_seen_rowid)` — the discovery watermark.
- `trigger_dispatches (consumer, source, item_id, times_triggered,
  last_triggered_at)` — per-item delivery progress for an item currently
  in-flight; `last_triggered_at` is `NULL` for a never-delivered item
  (due immediately). Due-ness is always computed live against the
  current policy, never stored as a future timestamp. The row is deleted
  once its repeat budget is exhausted.
- `item_policy_state (consumer, source, item_id, dispatch_policy)` — the
  `dispatch_policy` this consumer last saw for *every* item it's ever
  looked at (not just in-flight ones) — the baseline the "sync policy
  changes" step compares against to detect an edit, including on
  already-retired or backlog items.
- `dispatch_policies (name, repeat_times, interval_seconds, description)`
  — the centralized repeat/interval config `items.dispatch_policy`
  references by name.

## Tests

```bash
.venv/bin/pytest tests/ -v
```
