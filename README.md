# radiobeacon — CD3DXZ-1

*Versión en español: [README.es.md](README.es.md).*

**CD3DXZ-1** is an experimental VHF (2 m band) propagation beacon. It is a
radio station, not just a program: an amateur node — formerly an EchoLink
gateway — that now transmits automatically, on a fixed frequency, so other
operators can use its signal to check whether the 2 m path between their
station and this one is open.

Instead of sending only a plain identifier, the beacon puts *useful
content* on air: it pulls in public-interest bulletins (civil-protection
early warnings, earthquake reports), turns each one into a short
transmission, and plays it out over the repeater/simplex channel when the
frequency is idle. A listener who copies the beacon gets both a
propagation check and the bulletin itself.

Everything in this repository is the automation behind that station — the
data collection, the text preparation, and the on-air hand-off. Setting up
the transmitter, the sound interface and SvxLink itself is assumed already
done and is out of scope here.

---

## The station

| | |
|---|---|
| **Callsign** | CD3DXZ-1 |
| **Band** | VHF, 2 m (144–146 MHz) |
| **Mode** | Operator picks **one**: spoken **voice**, or **AX.25 packet** (1200-baud AFSK) |
| **Frequency / grid locator** | Set per install (station identity, entered in the dashboard) |
| **Transmitter control** | [SvxLink](https://www.svxlink.org/) — keys the radio, plays each clip only when the channel is clear |
| **Duty** | Disabled by default; the operator enables transmission explicitly |
| **Identification** | Callsign spoken/sent with every bulletin; a separate continuous CW ID (required by Chilean regulation) is noted as still to be added |

The beacon transmits **one content type at a time**. Switching between
voice and packet is a settings change, not a rebuild — the pending
transmit queue for the other type is cleared automatically.

### Voice

Each bulletin is spoken in Spanish by an offline text-to-speech engine
(Piper, neural; `espeak-ng` as a fallback). The spoken text is wrapped in
a fixed bulletin envelope — source name, date, and a closing line that
points listeners to official sources — so even a truncated bulletin is
still framed as an informational announcement and not mistaken for an
official channel.

### AX.25 packet

Each bulletin is sent as an AX.25 UI frame at 1200-baud AFSK, rendered to
audio with Direwolf's `gen_packets` tool (no live TNC, no network
connection). The frame uses an APRS-compatible message layout
(`CALLSIGN>DEST:text`) **but is not transmitted on the APRS calling
frequency** — it runs on a coordinated experimental frequency so it never
appears on the public APRS network. Long bulletins are split into several
frames; the frame length is held under the ~256-byte AX.25 limit
automatically.

---

## What goes on air

Content comes from configurable **sources**. Each source is polled on its
own interval, new items are stored, optionally shortened, and then queued
for transmission.

- **SENAPRED early warnings** — Chile's civil-protection early-warning
  alerts (weather, hydrological, geophysical events).
- **CSN earthquake reports** — seismic events published by the Centro
  Sismológico Nacional.
- **Any HTTP/JSON feed** — additional sources (weather APIs, local sensor
  readouts, etc.) are added from the dashboard by describing the endpoint
  and how to map its fields; no code change.

Every alert-type transmission is explicitly marked as an **experimental,
unofficial relay** and refers listeners to SENAPRED as the authoritative
source.

### From item to transmission

```
sources  →  stored item  →  (optional) AI summary, 2–3 sentences  →  sized for the chosen mode
                                                                      │
                                                          queued: N transmissions,
                                                          M seconds apart
                                                                      │
                                                      rendered to a WAV, one at a time
                                                                      │
                                            handed to SvxLink → played when the channel is idle
```

### The role of AI

AI does one narrow job here, and it is **off by default**: condensing a
bulletin so it fits an on-air transmission. A source item is often a full
web notice — too long to speak in a reasonable window or to fit in a
packet frame. When enabled, each new item is sent to a language model with
a fixed, operator-editable prompt (Spanish, "summarise this notice in 2–3
clear, complete sentences, invent nothing, leave no sentence unfinished").
The result is stored as the item's `summary` and is what the voice and
packet stages then use; the original text is kept untouched.

- **Provider-agnostic** — OpenAI, Claude, or a self-hosted Ollama model,
  chosen by config. This is the only step that makes an outbound call to
  an external service; with Ollama it stays fully local.
- **Only when it helps** — items already short enough are passed straight
  through, never sent to a provider.
- **No silent rewriting on failure** — if the model call fails the item is
  *not* transmitted, rather than going on air with unreviewed or partial
  text. A source can instead opt to fall back to its plain title
  (SENAPRED does).
- **Not a filter or a decision-maker** — it never chooses what to transmit,
  changes a transmit policy, or edits station identity. It only shortens
  text that a human-defined source already selected.

The model output is not length-capped after the fact — the prompt asks for
a complete short summary — so a badly-behaved model can still be caught by
the per-mode length limits downstream.

### Repeat behaviour

Each item is put on air a set number of times, a set interval apart —
these two numbers are a named **transmit policy**. Policies are edited
from the dashboard; an operator can override the policy on a single item,
or re-arm an item that has already finished its repeats. Every attempt
counts toward the repeat total, so a bulletin does not transmit forever if
the link is failing. The queue is stored on disk and survives a restart.

---

## Operating it

The dashboard (local web page, `http://127.0.0.1:8080`) is the operator
console:

- current beacon status — enabled/disabled, active mode, pending
  transmissions, last heartbeat, measured NTP clock offset;
- recent items and the on-air audit trail (what was transmitted, when, and
  the result);
- enable/disable transmission and switch mode without a restart;
- browse/search items, override a transmit policy, re-arm an item;
- manage sources and transmit policies.

Time is disciplined by the operating system's own NTP client. The beacon
additionally measures its clock offset against a public NTP server purely
to display it — it never adjusts the clock itself.

---

## Running the automation

```bash
./start.sh   # fresh clone: creates .env, sets up each component's venv,
             # and starts data collection + delivery + actions + the
             # dashboard + the beacon together (Ctrl+C stops all).
             # No secrets required; every default is safe to run as-is,
             # and the beacon starts disabled (BEACON_ENABLED=false).

./query_history.sh --help   # ad-hoc SQL against the local database
./stop.sh --status          # what's running, and its pid
```

Once running, open `http://127.0.0.1:8080`.

Each component can also be run on its own (`./data-adapters/start.sh`,
`./dispatcher/start.sh`, `./mq/start.sh`, `./actions/start.sh`,
`./ui/start.sh`, `./beacon/start.sh`) — each is self-contained and refuses
to start twice.

### How it's built

Seven decoupled components, wired together only by a shared SQLite
database (`storage/radiobeacon.db`) and, optionally, a local MQTT broker.
Each has its own README with the details.

`data-adapters/` doubles as the shared library — settings, the DB schema,
`timeutil`, `transmit_policy`, the LLM helpers — so every other component
installs it (`-e ../data-adapters` in its `requirements.txt`, which
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
Deploying SvxLink and `svxlink-txqueue` is out of scope — see
[documentation/svxlink-txqueue-SETUP.md](documentation/svxlink-txqueue-SETUP.md).
[CONTEXT.md](CONTEXT.md) records the original station design (the
interleaved voice/packet TDMA scheme that `beacon/` later replaced with
the simpler one-type-at-a-time model).

---

## Configuration

All configuration is a single `.env` file at the repo root (copy
`.env.example` to `.env`). Most settings are also editable live from the
dashboard's `/config` page. Variables specific to one component or source
are prefixed with its path (`ADAPTERS_SENAPRED_*`, `BEACON_*`); variables
read directly by a third-party SDK keep that SDK's own name.

The **station identity** (callsign, description, grid locator, frequency,
operator contact) is entered only in the dashboard and never read from the
environment — a stray `BEACON_CALLSIGN` in the shell can't put a wrong
callsign on air. The beacon refuses to transmit until every identity field
is filled in.

## Time is always UTC

Every datetime handled anywhere in this repo is timezone-aware UTC, with
no exceptions in storage, transmission scheduling, or internal
computation. The only place a local zone appears is text shown to a
person — spoken bulletin dates, the dashboard, human-readable logs — which
is converted to `DISPLAY_TIMEZONE` (default `America/Santiago`) at the
last step and never fed back into anything stored. See the code comments
in `adapters/timeutil.py` for the full rule.
