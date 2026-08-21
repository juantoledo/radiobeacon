# radiobeacon

Data pipeline for **CD3DXZ-1**, an experimental VHF (2m) propagation beacon
project. This repo currently covers the data side of that project: pulling
in raw data (starting with SENAPRED early-warning alerts) and storing it.
The radio/AX.25/voice side of the project is designed but not yet
implemented here — see [CONTEXT.md](CONTEXT.md) for the full system design.

## Layout

```
data-adapters/  fetches raw data from external sources, stores it in SQLite
dispatcher/     watches for new items and delivers them to handlers, run separately
mq/             optional local MQTT broker (Docker), dispatcher can publish CloudEvents here
actions/        optional MQTT-subscribed pipeline of N configurable actions (e.g. chunking)
storage/        the shared SQLite database (radiobeacon.db) both packages read/write
query_history.sh   ad hoc SQL queries against radiobeacon.db from the CLI
```

Each package folder has its own README with setup and usage details:
[data-adapters/README.md](data-adapters/README.md),
[dispatcher/README.md](dispatcher/README.md),
[mq/README.md](mq/README.md),
[actions/README.md](actions/README.md),
[storage/README.md](storage/README.md).

### Architecture

`data-adapters` and `dispatcher` are intentionally decoupled — data-adapters only
fetch and store raw data, dispatcher only watches for and delivers new
rows. Neither imports the other; they're wired together only by both
pointing at the same `storage/radiobeacon.db`. See [CONTEXT.md](CONTEXT.md)
for the broader layered design (adapters → aggregator → formatters → TX
layer) this project is working toward.

```
data-adapters (fetch)  →  storage/radiobeacon.db
                              ↑
                     dispatcher (watch + deliver, run separately)
                              ┊ (optional)
                     mq/ (Mosquitto, CloudEvents)
                              ┊ (optional)
                     actions/ (chained MQTT-subscribed pipeline)
```

## Quick start

```bash
cp .env.example .env   # fill in real values
./data-adapters/start.sh    # fetch + store from all adapters
./dispatcher/start.sh  # watch for new items and deliver them
./mq/start.sh          # optional — local broker for CloudEvents publishing
./actions/start.sh     # optional — run the configured action pipeline
./query_history.sh --help   # explore what's in radiobeacon.db
```

## Configuration

All configuration is via a single `.env` file at the repo root (copy
`.env.example` to `.env` and fill in real values — `.env` is gitignored).
Naming convention: vars specific to one package/adapter are prefixed with
its path (`ADAPTERS_SENAPRED_*`); generic vars read directly by a
third-party SDK under its own standard name are unprefixed.
