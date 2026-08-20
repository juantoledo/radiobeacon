# adapters

Fetches raw data from external sources and stores it in SQLite
(`storage/radiobeacon.db`). Adapters only fetch and store — no summarization, no
radio/AX.25 logic. Summarization is [enrichment](../enrichment/README.md)'s
job, run separately.

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
  [query_history.sh](../query_history.sh) at the repo root).

- **csn** (`src/adapters/csn/`) — recent earthquakes from Chile's Centro
  Sismológico Nacional (CSN, Universidad de Chile). CSN has no official
  public API; this uses a widely-used unofficial JSON mirror of its
  auto-detected earthquake list — no auth/key required, but the request
  needs a real browser `User-Agent` (the API's WAF 403s the default
  `Python-urllib/x.y` one). The API gives no stable id or per-event page,
  so `fecha` (the earthquake's detection timestamp) is used as both `id`
  and `event_key`, and every item's `url` points at the same
  sismologia.cl homepage.

New adapters are picked up automatically: `discover_adapters()`
(`src/adapters/__main__.py`) scans this package's submodules for concrete
`DataSourceAdapter` subclasses, so adding one just means adding a new
submodule — no registration step.

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
| `source_date_time` | when the source says the item happened/was published |

Items are immutable once stored — `store_reading()` uses `INSERT OR
IGNORE`, so an already-known `id` is never touched or refreshed, only
genuinely new items get inserted. This assumes an adapter never reuses an
id for content that changes over time (true for SENAPRED, see above).

## Setup

```bash
./start.sh
```

Creates a `.venv`, installs `requirements.txt`, loads `../.env` (if
present), and runs every discovered adapter once
(`PYTHONPATH=src python3 -m adapters`).

## Configuration

Env vars, in `.env` at the repo root (see `.env.example`). None are
secrets — all are optional and default to the current known-working value.

| var | default |
|---|---|
| `ADAPTERS_SENAPRED_IDENTITY_POOL_ID` | `us-east-1:17c696bc-53e1-49a2-991f-f1b65f752fda` |
| `ADAPTERS_SENAPRED_COGNITO_REGION` | `us-east-1` |
| `ADAPTERS_SENAPRED_APPSYNC_REGION` | `us-east-1` |
| `ADAPTERS_SENAPRED_APPSYNC_HOST` | `rz2uv7ifxbgflh2bqmp6kmh4le.appsync-api.us-east-1.amazonaws.com` |
| `ADAPTERS_SENAPRED_ALERTA_BASE_URL` | `https://senapred.cl/alerta/` |
| `ADAPTERS_SENAPRED_EVENTO_BASE_URL` | `https://senapred.cl/evento/` |
| `ADAPTERS_SENAPRED_QUERY_LIMIT` | `20` |
| `ADAPTERS_CSN_API_URL` | `https://api.gael.cloud/general/public/sismos` |
| `ADAPTERS_CSN_SITE_URL` | `https://www.sismologia.cl/` |

## Tests

```bash
.venv/bin/pytest tests/ -v                    # unit tests only
.venv/bin/pytest tests/ -v -m ""               # include the live integration tests
```

The `integration` marker (see `pytest.ini`) hits real external services
(SENAPRED, CSN) — excluded by default.
