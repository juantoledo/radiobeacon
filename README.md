# radiobeacon

Data pipeline (and, as of [beacon/](beacon/README.md), the transmission
orchestrator) for **CD3DXZ-1**, an experimental VHF (2m) propagation
beacon project: pulling in raw data (starting with SENAPRED early-warning
alerts and CSN earthquake reports), storing it, and delivering it to voice
(SvxLink) or AX.25 frame (Direwolf) channels on a TDMA schedule.
Deploying Direwolf/SvxLink themselves — their Dockerfiles, compose, and
the host-level audio setup CONTEXT.md describes — remains out of scope
here; `beacon/` assumes both already run as independently controllable
services on the host. See [CONTEXT.md](CONTEXT.md) for the full system
design.

## Layout

```
data-adapters/  fetches raw data from external sources, stores it in SQLite
dispatcher/     watches for new items and delivers them to handlers, run separately
mq/             optional local MQTT broker (Docker), dispatcher can publish CloudEvents here
actions/        optional MQTT-subscribed pipeline of N configurable actions (e.g. chunking)
ui/             server-rendered ops dashboard (FastAPI) — browse/override items, manage
                transmit policies, view the audit log; dockerizable, localhost-only by default
beacon/         TDMA transmission orchestrator — queues content, delivers it to voice
                (SvxLink) or AX.25 frame (Direwolf) channels; disabled by default
storage/        the shared SQLite database (radiobeacon.db) all packages read/write
query_history.sh   ad hoc SQL queries against radiobeacon.db from the CLI
start.sh        one-command fresh-clone setup — see Quick start below
stop.sh         stops running services (all, one, or --status) — see Quick start below
lib.sh          shared .env/.venv helpers sourced by this start.sh and every package's own start.sh
```

Each package folder has its own README with setup and usage details:
[data-adapters/README.md](data-adapters/README.md),
[dispatcher/README.md](dispatcher/README.md),
[mq/README.md](mq/README.md),
[actions/README.md](actions/README.md),
[ui/README.md](ui/README.md),
[beacon/README.md](beacon/README.md),
[storage/README.md](storage/README.md).

### Architecture

`data-adapters` and `dispatcher` are intentionally decoupled — data-adapters only
fetch and store raw data, dispatcher only watches for and delivers new
rows. Neither imports the other; they're wired together only by both
pointing at the same `storage/radiobeacon.db`. See [CONTEXT.md](CONTEXT.md)
for the broader layered design (adapters → aggregator → formatters → TX
layer) this project is working toward.

```
data-adapters (fetch)  →  storage/radiobeacon.db  ←  ui/ (browse + override, HTTP on localhost)
                              ↑
                     dispatcher (watch + deliver, run separately)
                              ┊ (optional)
                     mq/ (Mosquitto, CloudEvents)
                              ┊ (optional)
                     actions/ (chained MQTT-subscribed pipeline)
                              ┊ item.content_ready
                     beacon/ (TDMA voice/frame transmission orchestrator)
                              ┊ (needs real hardware, out of scope here)
                     SvxLink / Direwolf (voice / AX.25 radio TX)
```

## Quick start

```bash
./start.sh   # fresh clone: creates .env, sets up every .venv, starts
             # mq + data-adapters + dispatcher + actions + ui + beacon
             # together (Ctrl+C stops all of them). No secrets required
             # — every .env.example default is safe to run as-is, and
             # beacon starts disabled (BEACON_ENABLED=false).
./query_history.sh --help   # explore what's in radiobeacon.db
```

Once running, visit `http://127.0.0.1:8080` for the [ui/](ui/README.md)
dashboard.

To run just one piece instead of everything, use that package's own
`start.sh` directly (`./data-adapters/start.sh`, `./dispatcher/start.sh`,
`./mq/start.sh`, `./actions/start.sh`, `./ui/start.sh`, `./beacon/start.sh`)
— each is self-contained (creates its own `.venv` on first run) and
independent of the others. Each also refuses to start if it's already
running (tracked via a PID file under `run/`), so re-running one by
mistake errors instead of silently launching an untracked duplicate.

To stop things, use `./stop.sh` — it finds services by PID file, so it
works regardless of how they were started (`./start.sh`, an
individual `start.sh`, or a shell that's since closed):

```bash
./stop.sh                  # stop every service + mq
./stop.sh beacon actions   # stop only the named service(s)
./stop.sh --status         # show what's currently running, and its pid
```

## Configuration

All configuration is via a single `.env` file at the repo root (copy
`.env.example` to `.env` and fill in real values — `.env` is gitignored).
Naming convention: vars specific to one package/adapter are prefixed with
its path (`ADAPTERS_SENAPRED_*`); generic vars read directly by a
third-party SDK under its own standard name are unprefixed.

## Dates and times: always UTC

Every datetime that enters this repo — from an adapter's source, computed
internally, or read back out of storage — is UTC, always timezone-aware,
no exceptions. This applies uniformly across data-adapters, dispatcher,
and actions.

- Never call `datetime.now()` or `datetime.utcnow()` directly. Use
  `adapters.timeutil.utc_now()`.
- Never store or pass along a naive datetime from an external source
  without converting it first via `adapters.timeutil.to_utc()`, which
  requires you to state the source's timezone explicitly if it's naive
  — an unlabeled naive datetime silently treated as UTC is exactly the
  bug class this rule exists to prevent (senapred/csn's `fetched_at`
  originally used bare `datetime.now()`, which silently encoded the
  host's local timezone).
- `items.captured_at` and `audit_log.recorded_at` are populated by
  SQLite's own `datetime('now')`, which is correct in *value* (UTC) but
  uses SQLite's native string format (no offset marker) rather than
  Python's offset-suffixed ISO 8601. A deliberate, known format
  difference, not a bug — don't string-compare these columns against
  Python-generated ISO strings without normalizing first.
- One documented, permanent exception: CSN's `id`/`event_key` are
  derived from the *raw, unconverted* source timestamp string
  (`adapters.csn.CsnEarthquake.fecha`), not the UTC-converted
  `source_date_time` — intentional, to keep historical `items.item_id`
  values stable. Do not "fix" this to use the converted value; see the
  docstring on `CsnEarthquake.source_date_time`.
- Displaying a UTC datetime to a person (a future UI, a human-readable
  log/notification line) is the one legitimate reason to convert away
  from UTC — use `adapters.timeutil.to_display_tz()`, which converts to
  `DISPLAY_TIMEZONE` (env var, default `America/Santiago`; see
  `.env.example`). Presentation only: never feed its result back into
  anything stored or compared against other stored datetimes.
