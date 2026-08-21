# radiobeacon

Data pipeline for **CD3DXZ-1**, an experimental VHF (2m) propagation beacon
project. This repo currently covers the data side of that project: pulling
in raw data (starting with SENAPRED early-warning alerts), storing it, and
enriching it with AI-generated summaries. The radio/AX.25/voice side of the
project is designed but not yet implemented here — see [CONTEXT.md](CONTEXT.md)
for the full system design.

## Layout

```
adapters/     fetches raw data from external sources, stores it in SQLite
enrichment/   summarizes stored items via an LLM (Claude/OpenAI), run separately
dispatcher/   watches for new items and delivers them to handlers, run separately
storage/      the shared SQLite database (radiobeacon.db) all three packages read/write
query_history.sh   ad hoc SQL queries against radiobeacon.db from the CLI
```

Each package folder has its own README with setup and usage details:
[adapters/README.md](adapters/README.md), [enrichment/README.md](enrichment/README.md),
[dispatcher/README.md](dispatcher/README.md), [storage/README.md](storage/README.md).

### Architecture

`adapters`, `enrichment`, and `dispatcher` are intentionally decoupled —
adapters only fetch and store raw data, enrichment only reads/updates
already-stored rows, dispatcher only watches for and delivers new rows. None
of them import each other; they're wired together only by all pointing at
the same `storage/radiobeacon.db`. See [CONTEXT.md](CONTEXT.md) for the
broader layered design (adapters → aggregator → formatters → TX layer)
this project is working toward.

```
adapters (fetch)  →  storage/radiobeacon.db  ←  enrichment (summarize, run separately)
                              ↑
                     dispatcher (watch + deliver, run separately)
```

## Quick start

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY / OPENAI_API_KEY if using enrichment
./adapters/start.sh                       # fetch + store from all adapters
./enrichment/summarize_item.sh <item_id>  # summarize one stored item
./dispatcher/start.sh                     # watch for new items and deliver them
./query_history.sh --help                 # explore what's in radiobeacon.db
```

## Configuration

All configuration is via a single `.env` file at the repo root (copy
`.env.example` to `.env` and fill in real values — `.env` is gitignored).
Naming convention: vars specific to one package/adapter are prefixed with
its path (`ADAPTERS_SENAPRED_*`, `ENRICHMENT_SUMARIZER_*`); generic vars
read directly by a third-party SDK under its own standard name are
unprefixed (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`).
