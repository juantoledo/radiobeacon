# Architecture

How the pieces of radiobeacon fit together, how a Policy decides an item's
fetch/process/transmit behavior, and how to run the stack — either all
together or one component at a time.

## Components

Seven decoupled components, wired together only by a shared SQLite
database (`storage/radiobeacon.db`) and, optionally, a local MQTT broker.
Each has its own README with the details.

`data-adapters/` doubles as the shared library — settings, the DB schema,
`timeutil`, `policy`, the LLM helpers — so every other component installs
it (`-e ../data-adapters` in its `requirements.txt`, which the repo root's
`start.sh` picks up); `dispatcher/` is likewise installed by `ui/`. No
runtime coupling beyond that and the database.

```
data-adapters/  polls external sources, stores raw items          → data-adapters/README.md
dispatcher/     watches for new items, delivers them to handlers   → dispatcher/README.md
mq/             optional local MQTT broker (Docker)                → mq/README.md
actions/        optional MQTT pipeline (AI summary, chunking, …)   → actions/README.md
ui/             the operator dashboard (FastAPI, server-rendered)  → ui/README.md
beacon/         the transmission layer — renders + hands to SvxLink → beacon/README.md
storage/        the shared SQLite database                         → storage/README.md
```

`beacon/` renders each queued item to a single WAV and drops it into the
`svxlink-txqueue` spool folder; SvxLink plays it on the next idle channel.
Deploying SvxLink and `svxlink-txqueue` is out of scope here — see
[svxlink-txqueue-SETUP.md](svxlink-txqueue-SETUP.md). [../CONTEXT.md](../CONTEXT.md)
records the original station design (the interleaved voice/packet TDMA
scheme that `beacon/` later replaced with the simpler one-type-at-a-time
model).

## Policies

One named **Policy** is the whole definition of how an item behaves,
across three stages:

1. **Fetch** — how often the source is polled: once, every N seconds, or
   on a cron schedule.
2. **Process** — event-driven: the chunk + AI summary run once when new
   data arrives.
3. **Transmit** — how the item goes on air: once, N times a set interval
   apart, or on a cron schedule; every attempt counts toward the total, so
   a bulletin does not transmit forever if the link is failing.

An adapter points at exactly one Policy; a fresher item for the same
`event_key` supersedes an older one still queued. Policies are edited from
the dashboard; an operator can re-point a single item to a different whole
Policy, re-air it (same content), or reprocess it (re-run the AI). The
transmit queue is stored on disk and survives a restart.

## Running the stack

```bash
./start.sh                  # starts every component together (Ctrl+C stops all)
./query_history.sh --help   # ad-hoc SQL against the local database
./stop.sh --status          # what's running, and its pid
```

Each component can also be run on its own (`./data-adapters/start.sh`,
`./dispatcher/start.sh`, `./mq/start.sh`, `./actions/start.sh`,
`./ui/start.sh`, `./beacon/start.sh`) — each is self-contained and refuses
to start twice.
