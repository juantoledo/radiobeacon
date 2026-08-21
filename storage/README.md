# storage

Holds `radiobeacon.db`, the SQLite database shared by
[adapters](../adapters/README.md) (writes raw fetched items) and
[enrichment](../enrichment/README.md) (writes `summary` on existing rows).
Nothing in this folder is a package — no code lives here, just the
database file. `radiobeacon.db` is gitignored; only `.gitkeep` is tracked so the
folder exists before the first fetch creates it.

The schema and all read/write logic live in
`adapters/src/adapters/storage.py` (`get_connection()`,
`store_reading()`) — that module also owns the migration path for schema
changes, applied idempotently every time a connection is opened, so an
existing `radiobeacon.db` always stays compatible with the current code.

`get_connection()` opens the database in **WAL mode**: since each adapter
now runs as its own independent loop/thread (see
[adapters/README.md](../adapters/README.md)), each with its own connection
writing on its own schedule, WAL lets those writers coexist with readers
(e.g. `query_history.sh`) without blocking, and a longer busy-timeout
(30s) gives a writer more room to wait out another adapter's write instead
of failing outright on the rare occasion two fetches land at the same
moment. WAL adds two sidecar files next to the database
(`radiobeacon.db-wal`, `radiobeacon.db-shm`) — both gitignored, same as
the database itself.

## Schema (`items` table)

| column | meaning |
|---|---|
| `source`, `item_id` | primary key — which adapter, and the item's id from that source |
| `extracted_title`, `extracted_contents` | from the adapter's `title`/`contents` |
| `summary` | populated later by enrichment; `NULL` until then |
| `url` | public link for the item, if any |
| `event_key` | groups items that are updates to the same ongoing event |
| `type`, `subtype` | generic two-level category |
| `source_date_time` | when the source says the item happened/was published |
| `fetched_at` | when this fetch cycle ran |
| `captured_at` | when the row was inserted |
| `rawdata` | full JSON serialization of the original item |

Items are immutable once inserted (`INSERT OR IGNORE` — see
[adapters/README.md](../adapters/README.md#adapter-contract)), so
`captured_at`/`fetched_at` reflect the first time an item was seen, not the
most recent fetch cycle.

## Exploring the data

Use [`query_history.sh`](../query_history.sh) at the repo root rather than
querying `radiobeacon.db` directly — it wraps the common queries (event
timelines, most-active events, escalation detection, etc.):

```bash
../query_history.sh --help
```
