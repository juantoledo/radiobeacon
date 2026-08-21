# radiobeacon

Data pipeline for **CD3DXZ-1**, an experimental VHF (2m) propagation beacon
project. This repo currently covers the data side of that project: pulling
in raw data (starting with SENAPRED early-warning alerts) and storing it.
The radio/AX.25/voice side of the project is designed but not yet
implemented here — see [CONTEXT.md](CONTEXT.md) for the full system design.

## Layout

```
adapters/     fetches raw data from external sources, stores it in SQLite
dispatcher/   watches for new items and delivers them to handlers, run separately
storage/      the shared SQLite database (radiobeacon.db) both packages read/write
query_history.sh   ad hoc SQL queries against radiobeacon.db from the CLI
```

Each package folder has its own README with setup and usage details:
[adapters/README.md](adapters/README.md),
[dispatcher/README.md](dispatcher/README.md),
[storage/README.md](storage/README.md).

### Architecture

`adapters` and `dispatcher` are intentionally decoupled — adapters only
fetch and store raw data, dispatcher only watches for and delivers new
rows. Neither imports the other; they're wired together only by both
pointing at the same `storage/radiobeacon.db`. See [CONTEXT.md](CONTEXT.md)
for the broader layered design (adapters → aggregator → formatters → TX
layer) this project is working toward.

```
adapters (fetch)  →  storage/radiobeacon.db
                              ↑
                     dispatcher (watch + deliver, run separately)
```

## Quick start

```bash
cp .env.example .env   # fill in real values
./adapters/start.sh    # fetch + store from all adapters
./dispatcher/start.sh  # watch for new items and deliver them
./query_history.sh --help   # explore what's in radiobeacon.db
```

## Configuration

All configuration is via a single `.env` file at the repo root (copy
`.env.example` to `.env` and fill in real values — `.env` is gitignored).
Naming convention: vars specific to one package/adapter are prefixed with
its path (`ADAPTERS_SENAPRED_*`); generic vars read directly by a
third-party SDK under its own standard name are unprefixed.
