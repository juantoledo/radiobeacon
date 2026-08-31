# data-adapters

Fetches raw data from external sources and stores it in SQLite
(`storage/radiobeacon.db`). Adapters only fetch and store — no
radio/AX.25 logic.

## Adapter plugin types

Adapters are configured, not coded: each configured *instance* is a row in
the `adapter_instances` table (`source`, `adapter_type`, `enabled`,
`interval_seconds`, `config` — a JSON blob), managed from the UI's
`/adapters` page (or directly via `adapters.storage.set_adapter_instance`).
There are two adapter *types*, each a small generic class in this package
that interprets `config`:

- **`api`** (`src/adapters/api_adapter.py`, `ApiAdapter`) — calls an HTTP
  endpoint (`url`, `method`, `headers`, `query_params`, `body`) and maps the
  JSON response onto the item contract below entirely from `config`, parsed
  into the typed `ApiAdapterConfig`/`FieldMapping`/`DispatchPolicyRule`
  dataclasses (same module) rather than read via ad hoc `dict.get(...)`
  calls: locate the item list (`items_path`), then map each of the 7
  contract fields (`mapping.<name>`) via a single `str.format` **template**
  against the item's own raw fields, plus a `{uuid}` placeholder (see
  `_content_uuid` in `api_adapter.py`: a uuid5 derived from the item's own
  content, not `uuid.uuid4()` — the same item always produces the same
  value, so a source with no natural id can still get a stable one instead
  of a new random id flooding storage every poll) and `{source_name}` /
  `{source_url}` (this source's display name and site URL from the
  `sources` table — the same placeholders `beacon`/`actions` expose;
  fail-soft to the raw source key when unmanaged). One template covers
  every case: `{Fecha}` alone is a raw passthrough, plain text with no
  `{...}` is a constant, and `Sismo M{Magnitud} - {RefGeografica}` is a
  real construction — there's no separate "raw field"/"constant" mode,
  since a template already expresses both as special cases (earlier
  revisions had three, which turned out to be a false choice). The one
  thing a template alone can't express is `field_date_format`: when set on
  a mapping whose template is a *single bare* `{Name}` placeholder (nothing
  else in the string), that field's raw value is parsed with this strptime
  format and re-emitted as a naive ISO 8601 string without a timezone
  shift, used for a stable `id`/`event_key` (see the comment on
  `FieldMapping` in `api_adapter.py`); it's ignored for any other template
  shape. Separately: convert a raw timestamp field to UTC (`date_field`/
  `date_format`/`source_timezone`) for `source_date_time`, and optionally
  compute `transmit_policy` from a numeric threshold rule
  (`transmit_policy_rule`). No code required — a brand-new API-type source
  is entirely a config row, edited in the UI as discrete fields (url,
  method, header/query-param rows, a 7-row mapping table with drag-and-drop
  from a live response preview, date parsing, transmit-policy rule) — never as raw
  JSON. `FieldMapping.from_dict` still accepts the older three-key shape
  (`field`/`value`/`template`) transparently, migrating it to a template on
  read, so an already-saved instance never breaks across this change.

- **`custom`** (`src/adapters/custom_adapter.py`, `CustomAdapter`) —
  `config` is *exactly* `{"code": "..."}`, nothing else. The code is a full
  Python source string, trusted-admin-authored (this is a self-hosted,
  single-operator box — the snippet is `exec()`'d with no sandboxing), that
  must define `def fetch(config: dict) -> list[dict]`. Each returned dict's
  keys match the item contract (`id` required, the rest optional). Exists
  for sources whose fetch logic is genuine business logic — auth
  handshakes, request signing, multi-query merging — that can't be reduced
  to config-only URL/headers/mapping. Any tunable knobs a snippet needs
  (e.g. SENAPRED's Cognito/AppSync identity, see below) are plain Python
  constants inside the snippet's own source, not separate config keys — the
  UI's edit form for a CUSTOM instance is a single code editor, no other
  fields.

Both types read `config` fresh on every `fetch()` call — editing an
instance's config via the UI takes effect on the very next poll, no process
restart required (only adding/removing/disabling an *instance* needs the
long-running `adapters` process restarted, since that's what changes which
poll threads exist).

### Seeded instances: csn, senapred

A fresh database is seeded (see `_ensure_adapter_instances_seeded` in
`storage.py`) with two instances, reproducing what were previously
hand-written adapter modules:

- **csn** (`api`) — recent earthquakes from Chile's Centro Sismológico
  Nacional. CSN has no official public API; the seeded config points at a
  widely-used unofficial JSON mirror (no auth/key required, but needs a
  real browser `User-Agent` — the API's WAF 403s the default
  `Python-urllib/x.y` one). The API gives no stable id or per-event page,
  so the seeded config derives `id`/`event_key` from the raw `Fecha`
  timestamp field (`mapping.id.field_date_format`), and every item's `url`
  is a constant pointing at the sismologia.cl homepage. The seeded config
  sets no `transmit_policy_rule`, so every item resolves to the default
  `informational` policy; an operator can add a magnitude-threshold rule at
  `/adapters` if they want big quakes on a different tier.

- **senapred** (`custom`) — active early-warning alerts from senapred.cl.
  The seeded snippet (`storage._build_senapred_code`) uses senapred.cl's
  real backend: an AWS AppSync GraphQL API, reached via an anonymous
  Cognito Identity Pool (the same flow every visitor's browser uses — no
  login/API key required), querying both of SENAPRED's separate feeds
  (`alertasByDate` for "Alerta" items, `eventosByDate` for "Evento" items,
  e.g. seismic activity) since senapred.cl's own `/eventos/` page merges
  both. Its Cognito/AppSync identity (pool id, regions, host, base URLs,
  query limit) is baked into the generated snippet as plain constants, not
  read from a separate config — see "custom" above. SENAPRED publishes a
  new item (new id) for every update to an ongoing event rather than
  mutating one — `event_key` (SENAPRED's `urlAccess`) is the field shared
  by every item belonging to the same event, so its full timeline can be
  reconstructed with `WHERE event_key = ? ORDER BY source_date_time` (see
  [query_history.sh](../query_history.sh) at the repo root).
  The seeded snippet assigns every item `transmit_policy = "informational"`.

If this operator's database already had `ADAPTERS_CSN_*`/
`ADAPTERS_SENAPRED_*` settings overridden via the old `/config` groups
before this table existed, those values are folded forward automatically
(read via `get_setting` at seed time) rather than silently reverting to
the hardcoded defaults — for csn, into the seeded config's fields; for
senapred, baked as literals into the generated snippet's source (there's
no config left to put them in) — see `_SEED_ADAPTER_INSTANCES` in
`storage.py`.

## Sources table

`sources (source PRIMARY KEY, display_name, site_url, updated_at)` —
per-source display metadata, distinct from an adapter instance's own fetch
config (above) and from any one item's own `url` (for SENAPRED, a per-alert
link that changes every item). `beacon`'s `{source_name}`/`{source_url}`
template placeholders (`BEACON_FRAME_PREFIX`/`SUFFIX`, `BEACON_VOICE_PREFIX`/
`SUFFIX`/`TEMPLATE` — see [beacon/README.md](../beacon/README.md)) read
straight from this table via `adapters.storage.get_source_fields`; an
item's `source_name` is looked up fresh at transmit/chunk time, same as
everything else in that placeholder set. A source with no row falls back to
its own raw key as `source_name` and `""` as `source_url`, rather than
erroring.

Seeded with `csn`/`senapred` on the very first `get_connection()` call
against a database (see `_ensure_sources_seeded` in `storage.py`) — never
re-seeded or reset once the table has any row, so an edit or deletion
sticks. The UI's `/adapters` create/edit form writes to this table too (so
giving an adapter instance a display name is one step, not two), or manage
it directly with `sources.sh`, mirroring `dispatcher/policies.sh`'s shape:

```bash
./sources.sh list
./sources.sh set <source> --display-name "..." [--site-url "..."]
./sources.sh delete <source>
```

## Adapter contract

Every adapter type subclasses `DataSourceAdapter` (`src/adapters/base.py`)
and implements `fetch() -> SourceReading`. `fetch_and_store()` (inherited,
not overridden) fetches then persists via `storage.store_reading()`.

`ApiAdapter`/`CustomAdapter` both build `SourceReading.data` as a list of
`AdapterItem` (`src/adapters/base.py`) — the formalized version of the
generic contract `storage.store_reading()` has always read via
`getattr(item, name, None)`:

| field | meaning |
|---|---|
| `id` | required — items without one are skipped, not stored |
| `title`, `contents` | mapped to `extracted_title` / `extracted_contents` |
| `url` | public link for the item, if any |
| `event_key` | groups items that are updates to the same ongoing thing |
| `type`, `subtype` | generic two-level category (raw API type + finer category) |
| `transmit_policy` | names a row in the `transmit_policies` table (owned by this package — created + seeded by `get_connection()`, same as `sources`) — a soft reference that centralizes how many times, and how far apart, [beacon](../beacon/README.md) puts the item on air |
| `source_date_time` | when the source says the item happened/was published |
| `raw` | the original, unmapped item as returned by the source — carried alongside the mapped fields so `rawdata` (`store_reading` dumps the whole `AdapterItem`) keeps the true raw payload, not just its mapped view |

Items are immutable once stored — `store_reading()` uses `INSERT OR
IGNORE`, so an already-known `id` is never touched or refreshed, only
genuinely new items get inserted. This assumes an adapter never reuses an
id for content that changes over time (true for SENAPRED, see above).
`transmit_policy` is the exception: like `summary`, adapters only ever
propose an initial value for it — it may be updated afterward by a
separate actor (see
[dispatcher/override_item.py](../dispatcher/README.md#manual-overrides--rearm)).

## Setup

```bash
./start.sh
```

Creates a `.venv`, installs `requirements.txt`, loads `../.env` (if
present), then `exec`s into `PYTHONPATH=src python3 -m adapters` — a
**long-running process**, not a one-shot script. Every enabled
`adapter_instances` row runs as its own loop, on its own thread, on its own
polling interval (its `interval_seconds`, or `ADAPTERS_DEFAULT_INTERVAL_SECONDS`
if unset). `Ctrl+C` (or `SIGTERM`) stops every adapter's loop and exits
cleanly.

For a single one-off fetch instead (e.g. for testing), fetch one configured
instance once and exit: `PYTHONPATH=src python3 -m adapters --once csn`. The
UI's `/adapters` edit page also has a "Test fetch" button that runs
`.fetch()` once (without storing) against whatever config is currently in
the form, so a new or edited instance can be validated before saving.

## Configuration

- `ADAPTERS_DEFAULT_INTERVAL_SECONDS` (env/`/config`, default `10`) —
  fallback poll interval for any adapter instance without its own
  `interval_seconds`.
- Everything else — which instances exist, their type, their `config`,
  whether they're enabled, their own interval — lives in the
  `adapter_instances` table, managed at `/adapters` in the UI.

## Tests

```bash
.venv/bin/pytest tests/ -v                    # unit tests only
.venv/bin/pytest tests/ -v -m ""               # include the live integration tests
```

The `integration` marker (see `pytest.ini`) hits real external services
(SENAPRED, CSN) — excluded by default.
