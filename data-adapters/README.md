# data-adapters

Fetches raw data from external sources and stores it in SQLite
(`storage/radiobeacon.db`). Adapters only fetch and store — no
radio/AX.25 logic.

## Adapters

- **senapred** (`src/adapters/senapred/`) — active early-warning alerts from
  senapred.cl. Uses senapred.cl's real backend: an AWS AppSync GraphQL API,
  reached via an anonymous Cognito Identity Pool (the same flow every
  visitor's browser uses — no login/API key required). Queries both of
  SENAPRED's separate feeds (`alertasByDate` for "Alerta" items,
  `eventosByDate` for "Evento" items, e.g. seismic activity) since
  senapred.cl's own `/eventos/` page merges both.

  SENAPRED publishes a new item (new id) for every update to an ongoing
  event (declared → monitored → modified → cancelled) rather than mutating
  one — `event_key` (SENAPRED's `urlAccess`) is the field shared by every
  item belonging to the same event, so its full timeline can be
  reconstructed with `WHERE event_key = ? ORDER BY source_date_time` (see
  [query_history.sh](../query_history.sh) at the repo root). `dispatch_policy`
  is `"urgent"` for `"Alerta"` items (active early-warnings) and
  `"informational"` for `"Evento"` items (monitoring/historical feed).

- **csn** (`src/adapters/csn/`) — recent earthquakes from Chile's Centro
  Sismológico Nacional (CSN, Universidad de Chile). CSN has no official
  public API; this uses a widely-used unofficial JSON mirror of its
  auto-detected earthquake list — no auth/key required, but the request
  needs a real browser `User-Agent` (the API's WAF 403s the default
  `Python-urllib/x.y` one). The API gives no stable id or per-event page,
  so `fecha` (the earthquake's detection timestamp) is used as both `id`
  and `event_key`, and every item's `url` points at the same
  sismologia.cl homepage. `dispatch_policy` is `"urgent"` at/above
  `ADAPTERS_CSN_URGENT_MAGNITUDE_THRESHOLD`, `"informational"` below it —
  see [dispatcher/README.md](../dispatcher/README.md#delivery-repeat-policy-dispatch_policy).

New adapters are picked up automatically: `discover_adapters()`
(`src/adapters/__main__.py`) scans this package's submodules for concrete
`DataSourceAdapter` subclasses, so adding one just means adding a new
submodule — no registration step.

## Sources table

`sources (source PRIMARY KEY, display_name, site_url, updated_at)` —
per-source display metadata, distinct from an adapter's own internal
settings (`ADAPTERS_CSN_*`/`ADAPTERS_SENAPRED_*`, above) and from any one
item's own `url` (for SENAPRED, a per-alert link that changes every
item). `beacon`'s `{source_name}`/`{source_url}` template placeholders
(`BEACON_FRAME_PREFIX`/`SUFFIX`, `BEACON_VOICE_PREFIX`/`SUFFIX`/`TEMPLATE`
— see [beacon/README.md](../beacon/README.md)) read straight from this
table via `adapters.storage.get_source_fields`; an item's `source_name`
is looked up fresh at transmit/chunk time, same as everything else in
that placeholder set. A source with no row falls back to its own raw key
as `source_name` and `""` as `source_url`, rather than erroring.

Seeded with `csn`/`senapred` on the very first `get_connection()` call
against a database (see `_ensure_sources_seeded` in `storage.py`) — never
re-seeded or reset once the table has any row, so an edit or deletion
sticks. Managed with `sources.sh`, mirroring `dispatcher/policies.sh`'s
shape exactly:

```bash
./sources.sh list
./sources.sh set <source> --display-name "..." [--site-url "..."]
./sources.sh delete <source>
```

## Adapter contract

Every adapter subclasses `DataSourceAdapter` (`src/adapters/base.py`) and
implements `fetch() -> SourceReading`. `fetch_and_store()` (inherited, not
overridden) fetches then persists via `storage.store_reading()`.

Items in `SourceReading.data` are duck-typed against a generic contract —
each of these is optional, read via `getattr(item, name, None)`:

| property | meaning |
|---|---|
| `id` | required — items without one are skipped, not stored |
| `title`, `contents` | mapped to `extracted_title` / `extracted_contents` |
| `url` | public link for the item, if any |
| `event_key` | groups items that are updates to the same ongoing thing |
| `type`, `subtype` | generic two-level category (raw API type + finer category) |
| `dispatch_policy` | names a row in [dispatcher](../dispatcher/README.md)'s `dispatch_policies` table — a soft reference, meaningless to this package, that centralizes delivery repeat count/interval config |
| `source_date_time` | when the source says the item happened/was published |

Items are immutable once stored — `store_reading()` uses `INSERT OR
IGNORE`, so an already-known `id` is never touched or refreshed, only
genuinely new items get inserted. This assumes an adapter never reuses an
id for content that changes over time (true for SENAPRED, see above).
`dispatch_policy` is the exception: like `summary`, adapters only ever
propose an initial value for it — it may be updated afterward by a
separate actor (see
[dispatcher/override_item.py](../dispatcher/README.md#manual-overrides-take-effect-immediately-mostly)).

## Setup

```bash
./start.sh
```

Creates a `.venv`, installs `requirements.txt`, loads `../.env` (if
present), then `exec`s into `PYTHONPATH=src python3 -m adapters` — a
**long-running process**, not a one-shot script. Every discovered adapter
runs as its own loop, on its own thread, on its own polling interval (see
`ADAPTERS_<NAME>_INTERVAL_SECONDS` below), all in parallel. `Ctrl+C`
(or `SIGTERM`) stops every adapter's loop and exits cleanly.

For a single one-off fetch instead (e.g. for testing), run one adapter's
module directly: `PYTHONPATH=src python3 -m adapters.senapred`.

## Configuration

Env vars, in `.env` at the repo root (see `.env.example`). None are
secrets — all are optional and default to the current known-working value.

| var | default |
|---|---|
| `ADAPTERS_DEFAULT_INTERVAL_SECONDS` | `10` — fallback poll interval for any adapter without its own `ADAPTERS_<NAME>_INTERVAL_SECONDS` |
| `ADAPTERS_SENAPRED_INTERVAL_SECONDS` | `10` |
| `ADAPTERS_SENAPRED_IDENTITY_POOL_ID` | `us-east-1:17c696bc-53e1-49a2-991f-f1b65f752fda` |
| `ADAPTERS_SENAPRED_COGNITO_REGION` | `us-east-1` |
| `ADAPTERS_SENAPRED_APPSYNC_REGION` | `us-east-1` |
| `ADAPTERS_SENAPRED_APPSYNC_HOST` | `rz2uv7ifxbgflh2bqmp6kmh4le.appsync-api.us-east-1.amazonaws.com` |
| `ADAPTERS_SENAPRED_ALERTA_BASE_URL` | `https://senapred.cl/alerta/` |
| `ADAPTERS_SENAPRED_EVENTO_BASE_URL` | `https://senapred.cl/evento/` |
| `ADAPTERS_SENAPRED_QUERY_LIMIT` | `20` |
| `ADAPTERS_CSN_INTERVAL_SECONDS` | `10` |
| `ADAPTERS_CSN_API_URL` | `https://api.gael.cloud/general/public/sismos` |
| `ADAPTERS_CSN_SITE_URL` | `https://www.sismologia.cl/` |
| `ADAPTERS_CSN_URGENT_MAGNITUDE_THRESHOLD` | `4.5` |
| `ADAPTERS_CSN_SOURCE_TZ` | `America/Santiago` — best-effort assumption about the timezone of CSN's unlabeled `Fecha` field; see the comment on `CsnEarthquake.source_date_time` |

## Tests

```bash
.venv/bin/pytest tests/ -v                    # unit tests only
.venv/bin/pytest tests/ -v -m ""               # include the live integration tests
```

The `integration` marker (see `pytest.ini`) hits real external services
(SENAPRED, CSN) — excluded by default.
