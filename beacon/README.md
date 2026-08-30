# beacon

The TDMA transmission orchestrator for the CD3DXZ-1 experimental VHF
propagation beacon — the RF/transmission layer CONTEXT.md describes as
"designed but not implemented." This is that layer: it schedules content
arriving from the rest of the pipeline into a durable transmit schedule
and delivers it to a voice channel (SvxLink) or an AX.25 frame channel
(Direwolf), inside a configurable, repeating time window, since the two
channels compete for the same audio hardware and can't transmit
simultaneously. Each item is put on air `repeat_times` times, spaced
`interval_seconds` apart — the numbers its `transmit_policy` names (see
"Transmit schedule" below).

Not the same thing as `ui/src/ui/beacon.py` — that module is the "beacon
identity" settings page (`/beacon`'s callsign/description fields). No
Python import connects the two; they only share the DB.

## Content flow

```
dispatcher --item.dispatched--> actions.ai --item.ai_settled--> actions.chunk --item.chunked--\
                                                                                                 \-- actions.content_ready --item.content_ready--> beacon (beacon_tx_schedule)
```

A linear pipeline, not a race: `actions.ai` always runs first (and
always publishes, whether or not it actually produces a summary — see
[actions/README.md](../actions/README.md)); `actions.chunk` subscribes to
`ai`'s output rather than `item.dispatched` directly, so it always runs
*after* `ai` has settled, chunking `items.summary` when one exists and
falling back to `extracted_contents` otherwise. By the time
`actions.content_ready` fires `item.content_ready` (published once —
and only once — both `chunk` and `ai` have finished, mostly
defense-in-depth at this point given the ordering above), the `chunks`
table already holds the best available content, correctly sized. Beacon's
job on that event is therefore simple — insert (or reset, on a rearm) rows
into `beacon_tx_schedule`, snapshotting the item's `transmit_policy` name:

- **Frame**: one `kind="frame"` row per row in `chunks` (`ref` =
  `str(chunk_index)`), always — no special-casing for whether a summary
  existed, since `chunks` already reflects it.
- **Voice**: always one `kind="voice"` row (`ref = ""`), resolved via
  `items.summary` at transmit time.

**MQTT delivery alone isn't trusted as the sole path in.** beacon's MQTT
client uses `clean_session=True` (see `_run_mqtt_client`'s docstring for
why — a persistent session's stale subscriptions caused real
double-transmission earlier), which means a message published while
beacon is momentarily disconnected is simply gone at the MQTT layer, not
queued by the broker. `_reconcile_missed_content_ready` closes that gap:
periodically (`BEACON_CONTENT_READY_RECONCILE_INTERVAL_SECONDS`, default
30s — plus once immediately on startup, which is what actually matters
most), it compares `actions.content_ready`'s own durable publish record
(`item_readiness`) against beacon's own `beacon.content_ready.enqueued`
audit trail, and schedules anything genuinely missed. Confirmed live: on
one restart, `actions.content_ready` published a backlog of 52 items
faster than beacon's client finished connecting — all 52 would have been
silently lost without this. (Since the schedule table is now durable, a
beacon restart no longer loses rows already scheduled — but reconcile
still covers publishes that landed while beacon was down.)

Text resolution stays lazy (not baked in at schedule time) for both: a
later rearm, or a summary changing between schedule and transmit, is
naturally reflected — whatever's true right now is what gets sent.

Frame's length limit is **not** a new beacon-specific setting — it reuses
`ACTIONS_CHUNK_MAX_CHARS`, itself a *ceiling*, not a fixed size —
`actions.chunk` dynamically clamps it down further at runtime
(`data-adapters/src/adapters/ax25.py`) against the current
`BEACON_CALLSIGN`/`BEACON_FRAME_DESTINATION`/`BEACON_FRAME_PREFIX`/
`BEACON_FRAME_SUFFIX`, so an assembled frame can't silently exceed
AX.25's ~256-byte limit and get dropped
(`beacon.frame.dropped_too_long`) regardless of how those change later.

Voice's length limit **is** its own dedicated setting,
`BEACON_VOICE_MAX_CHARS` — deliberately not `ACTIONS_AI_MAX_CHARS`, whose
job is gating whether `actions.ai`'s LLM call runs at all, not bounding how
much of `items.summary` (always populated — see `actions/README.md`) voice
actually speaks. It's a time-budget cap, not a protocol limit like frame's:
text beyond it is word-boundary truncated to the first piece and the rest
is silently dropped, no part markers.

## TDMA schedule

One repeating cycle, split into slots in this fixed order: **voice**,
**guard** (a gap, letting the outgoing transmitter release the shared
audio device before the next one opens it), **frame**, then **idle** for
whatever's left of the cycle. Defaults: 90s total, 60s voice, 30s frame,
0s guard (collapses voice straight into frame) — all configurable via
`/config` → "Beacon — Schedule", including live, no restart needed.

Timing is recalculated from scratch every tick (`src/beacon/schedule.py`),
anchored to `now % total_seconds` — never accumulated sleeps. This is also
the entirety of beacon's NTP story: the OS's own NTP daemon
(systemd-timesyncd/chronyd) does actual clock discipline; if it steps or
slews the system clock, the very next tick already reflects it, with no
custom sync code needed. `src/beacon/ntp.py` adds a purely optional,
read-only visibility layer on top — periodically checking the measured
offset against a real NTP server for the `/beacon` status page — and
never adjusts anything itself.

Every tick, `_write_heartbeat` persists the current `SlotState` (already
computed by `schedule.py`, nothing new derived) to `beacon_status`:
`current_slot`, `current_cycle_index`, `current_cycle_elapsed_seconds`,
`current_slot_remaining_seconds`, plus each kind's pending
`beacon_tx_schedule` row count (still written under the historical
`voice_queue_depth`/`frame_queue_depth` keys; `*_dropped_total` is always
`0` now and kept for one release only). `ui/`'s `/beacon` page reads these
to render a live cycle timeline and slot countdown — see
[ui/README.md](../ui/README.md#live-dashboard).

**Content is acted on as soon as it's scheduled, not just at a slot's
start.** `_handle_content_ready_event` sets a `wake_event` right after
inserting the schedule rows, so `_run_tdma_loop`'s tick-wait (normally
`BEACON_TICK_SECONDS`) returns almost immediately. `_drain_kind` is
attempted on every iteration the current slot matches, a cheap query when
nothing of that kind is due.

**A slot's length is a floor, not a hard ceiling.** Whenever it runs,
`_drain_kind` transmits *every* currently-due row of that kind — pausing
`BEACON_VOICE_INTER_TX_DELAY_SECONDS`/`BEACON_FRAME_INTER_TX_DELAY_SECONDS`
(default 2s each) between transmissions so PTT can release/re-key. If it
runs past the slot's nominal end, it finishes before handing control back.
This blocks `_maybe_control_services`, the NTP check, and the status
heartbeat for that duration; `BEACON_QUEUE_MAX_SIZE` (default 200 — sized
for frame content, where one row is one *chunk*, not one item; a real
SENAPRED report has been observed producing 57 chunks) caps how many
`beacon_tx_schedule` rows of a kind can be pending — `add_tx_schedule_unit`
trims the oldest by `created_at` beyond that. Read fresh every
`item.content_ready`, so a `/config` edit takes effect on the next
scheduled item.

One known edge case this doesn't address: if a transmission overruns far
enough to blow through `BEACON_SLOT_LEAD_TIME_SECONDS` before the *next*
slot's own start, `_maybe_control_services` can miss that occurrence's
service switch entirely — its lead-time trigger only fires on a small
*positive* ETA, not on "we're already past due." Pre-existing (a large
backlog at slot start could already overrun this way); now reachable from
any point in a slot, not just its start.

## Transmit schedule (durable repeat)

`beacon_tx_schedule` (owned by
[`adapters.storage`](../data-adapters/src/adapters/storage.py)) is the
source of truth for what beacon still has to put on air:

```sql
CREATE TABLE beacon_tx_schedule (
    source              TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    kind                TEXT NOT NULL,             -- 'frame' | 'voice' | future
    ref                 TEXT NOT NULL DEFAULT '',  -- frame = str(chunk_index); voice = ''
    transmit_policy     TEXT,                      -- policy NAME snapshot
    sent_count          INTEGER NOT NULL DEFAULT 0,
    last_transmitted_at TEXT,                      -- NULL = never sent (due now)
    enqueued_event_id   TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, item_id, kind, ref)
);
```

- **Durable** — survives a beacon restart (unlike the in-memory queues it
  replaced). `_reconcile_missed_content_ready` still covers publishes that
  landed while beacon was down.
- **Live policy** — every cycle, `_drain_kind` resolves
  `adapters.transmit_policy.policy_for(row.transmit_policy)` fresh.
  Due iff `last_transmitted_at IS NULL` or
  `utc_now() >= last_transmitted_at + interval_seconds`. Retired (row
  deleted, `beacon.tx.retired` recorded) iff `sent_count >= repeat_times`.
  Editing a tier in `transmit_policies` changes in-flight behavior on the
  next cycle.
- **Every attempt counts** — `sent_count` increments whether the KISS send
  / TTS+TX succeeded or not, so a persistently failing link still retires
  the row instead of wedging the slot. Mirrors the old `_try_transmit_*`.
- **Rearm / policy change** — a fresh `item.content_ready` (new CloudEvent
  id) PK-upserts each row back to `sent_count = 0`,
  `last_transmitted_at = NULL`, refreshing the policy name.
- **Kind-generic** — `SLOT_KINDS` maps a `schedule.Slot` to the kinds it
  transmits; `KIND_TRANSMITTERS` maps a kind to its `(conn, row, ctx) ->
  bool` transmitter. A future repeatable TDMA slot type is a new `Slot`
  value plus one entry in each — `_drain_kind` never special-cases.

## Enable / disable ("start/stop/restart")

Not real OS process control. `beacon` runs as a fifth long-lived process
(started by the repo root's `start.sh`, alongside `data-adapters`/`dispatcher`/
`actions`/`ui`). The `/beacon` page's Enable/Disable button just flips the
`BEACON_ENABLED` settings flag, which the running process re-reads every
tick — same pattern `ACTIONS_AI_ENABLED` already uses elsewhere in this
repo. "Restart" means the next tick picks up whatever's in `settings`
now; nothing kills or respawns the actual process. Deliberate: this avoids
giving the still-auth-less UI the power to spawn/kill OS processes.
Schedule rows keep being written even while disabled — only the transmit
step checks the flag.

## Audio-device contention & service control

CONTEXT.md flags SvxLink/Direwolf fighting over the same audio hardware as
a "hallazgo crítico," with three candidate strategies, none chosen. This
package resolves it by actively stopping/starting each service around its
slot (`src/beacon/service_control.py`) — before a content slot begins
(with a configurable lead time, `BEACON_SLOT_LEAD_TIME_SECONDS`, standing
in for CONTEXT.md's own unmeasured `PTT_LATENCY_S`/`TNC_LATENCY_S`), and
only when there's actually a pending `beacon_tx_schedule` row for it.

**`BEACON_SERVICE_CONTROLLER=logging`** (default) just logs what it would
do — the only path verifiable without the real shack host.
**`=systemctl`** actually shells out to `systemctl start|stop <name>` —
this requires beacon's own process to have permission to control those
specific units, typically a passwordless sudoers rule scoped to exactly
`systemctl {start,stop} svxlink` and `systemctl {start,stop} direwolf`
(**not** unrestricted sudo). A materially more privileged capability than
anything else in this codebase, which otherwise only touches its own
SQLite DB and makes outbound network calls — set this up deliberately on
the host, it does not work out of the box.

**Deploying Direwolf/SvxLink themselves — their Dockerfiles, compose, or
the host-level ALSA audio provisioning CONTEXT.md also describes — is out
of scope for this package.** `beacon` assumes both already run as
independently start/stoppable services on the host.

## Voice: TTS is real, SvxLink control is an honest stub

`src/beacon/voice.py`'s `synthesize_speech` turns resolved voice text into
a WAV file — real and testable, via either of two selectable engines
(`BEACON_TTS_ENGINE`):

- **`piper`** (default) — shells out to `piper` (`piper-tts`, already in
  `requirements.txt`; offline neural TTS, no API key). Sounds much more
  natural than `espeak`. Needs a voice model (`BEACON_TTS_PIPER_MODEL`, a
  `.onnx` file with its `.onnx.json` sidecar alongside it — see
  [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) for
  others). Text is piped over stdin, matching piper's own CLI contract.
  The default model (`es_MX-claude-high`, Latin American/Mexican Spanish,
  ~60MB) isn't
  committed to git (`beacon/storage/piper_voices/` is gitignored, same
  treatment as `.venv/`) — `start.sh` downloads it automatically on first
  run via `ensure_default_piper_voice` in `../lib.sh` whenever
  `BEACON_TTS_ENGINE=piper` and `BEACON_TTS_PIPER_MODEL` is left at its
  default path; a failed/offline download just logs a warning and leaves
  piper erroring at synthesis time (non-fatal — same as any other
  misconfigured `BEACON_TTS_PIPER_MODEL`) rather than blocking startup.
- **`espeak`** — shells out to `espeak-ng` (offline, no API key, standard
  on Debian/Ubuntu — `apt install espeak-ng`). Zero setup, offline-safe
  fallback, but sounds noticeably robotic (formant synthesis, not
  neural).

`BEACON_VOICE_TRANSMITTER=logging` (default) just logs what it would
play. `=svxlink` is an **unverified stub** — `SvxlinkControlTransmitter`
raises `NotImplementedError` unconditionally. CONTEXT.md itself marks
SvxLink's remote-control mechanism as an open TODO ("TCL events o comando
remoto"); the most likely real approach (SvxLink's TCL event handler +
its `playFile` command) is documented as a comment, not implemented. Do
not remove that `NotImplementedError` without validating against a real
SvxLink instance on the actual shack host first.

## AX.25 / Direwolf

`src/beacon/kiss.py` is a real, hand-built KISS framing + AX.25 UI-frame
encoder + TCP client for Direwolf's KISS port (default 8001) — no
third-party AX.25 library. Fully unit-tested against a real localhost TCP
socket standing in for Direwolf. Not field-validated: CONTEXT.md's own
AX.25 example was confirmed working via `kissutil`'s own text-to-bytes
translation, not this hand-rolled encoder talking to the raw socket — one
real on-air/lab smoke test is recommended before first live use, in
addition to (not instead of) the unit suite.

`BEACON_FRAME_PREFIX`/`BEACON_FRAME_SUFFIX` (both empty by default) wrap
the actual transmitted frame payload — distinct from
`BEACON_FRAME_DESTINATION`, which only labels the AX.25 tocall address,
not content a listener decodes. `BEACON_VOICE_PREFIX`/`BEACON_VOICE_SUFFIX`
are the equivalent for voice — separate settings, wrapping the spoken text
before it's substituted into `BEACON_VOICE_TEMPLATE`'s `{text}`, added
outside `BEACON_VOICE_MAX_CHARS`'s truncation budget the same way frame's
wrap an already-sized chunk. Frame's prefix/suffix are applied to every
frame, including each chunk of a multi-frame item (not just once per
item), so a listener catching only one frame still sees it; counts
against the same 256-byte `FrameTooLongError` ceiling as the rest of the
frame (see `formatters.py`).

All four (plus `BEACON_VOICE_TEMPLATE` itself) are str.format templates,
not plain literals. Beyond `{date}` (`items.source_date_time`, converted
to `DISPLAY_TIMEZONE` and formatted per `BEACON_DATE_FORMAT`), each also
accepts item-derived placeholders — `{source}`, `{item_id}`, `{type}`,
`{subtype}`, `{extracted_title}`, `{url}` (`content.resolve_item_fields`)
— resolved fresh at transmit time, same as `text` itself. Two more,
`{source_name}`/`{source_url}`, are per-*source* rather than per-item — a
display name ("Centro Sismológico Nacional" for `csn`) and general site
URL, read from the `sources` table (`adapters.storage` in
`data-adapters` — seeded with `csn`/`senapred` on first run, managed live
via `data-adapters/sources.sh list`/`set`/`delete`) rather than the
item's own row. Distinct from `{source}` (the raw internal key, e.g.
`"csn"`) and `{url}` (this specific item's own link — for SENAPRED, a
per-alert URL that differs from `{source_url}`'s general homepage).
Rendered via `adapters.templating.safe_format`: an invalid placeholder
(a typo'd setting) logs an error and falls back to `""` rather than
raising — the TDMA tick loop has no per-tick catch-all, so an uncaught
exception here would otherwise kill the whole transmit thread until
restart. `actions.chunk`'s dynamic `ACTIONS_CHUNK_MAX_CHARS` clamp
(`data-adapters/src/adapters/ax25.py`) accounts for the CURRENT item's
real rendered prefix/suffix — `{date}` and every item/source field — not
the raw template's length, so neither a short `" {date}"` suffix nor a
long `{extracted_title}`/`{source_name}` can silently under-clamp and
cause an overflow at transmit time.

## Not addressed

Continuous, content-independent periodic station identification (CW ID)
— CONTEXT.md's regulatory section requires "mantener identificación en CW
según normativa del país," a third content channel (Morse code) distinct
from both voice and AX.25 frames. This package's schedule-driven design
("only transmit when there's a pending row") doesn't produce that on its
own — a real compliance gap worth a follow-up, not silently decided here.

## Setup

```bash
./start.sh
```

Same pattern as every other package: creates `.venv`, installs
`requirements.txt`, loads `../.env`, `exec`s into
`PYTHONPATH=src python3 -m beacon` — a long-running process.
`Ctrl+C`/`SIGTERM` stops it cleanly.

## Configuration

Env vars, in `.env` at the repo root (see `.env.example`) — also all
editable live via `/config` → the "Beacon — *" groups, except the beacon
identity fields (`BEACON_CALLSIGN`, etc. — DB-only, never `.env`, set via
`/beacon` itself; `beacon` refuses to transmit without a configured
callsign, independent of `BEACON_ENABLED`, since station ID is a
regulatory requirement).

| var | default |
|---|---|
| `BEACON_ENABLED` | `false` |
| `BEACON_WINDOW_TOTAL_SECONDS` | `90` |
| `BEACON_WINDOW_VOICE_SECONDS` | `60` |
| `BEACON_WINDOW_FRAME_SECONDS` | `30` |
| `BEACON_WINDOW_GUARD_SECONDS` | `0` |
| `BEACON_TICK_SECONDS` | `1` |
| `BEACON_SLOT_LEAD_TIME_SECONDS` | `2` |
| `BEACON_VOICE_INTER_TX_DELAY_SECONDS` | `2` |
| `BEACON_FRAME_INTER_TX_DELAY_SECONDS` | `2` |
| `BEACON_VOICE_TEMPLATE` | `{callsign}. {text}. {date}` |
| `BEACON_VOICE_PREFIX` / `BEACON_VOICE_SUFFIX` | `""` / `""` |
| `BEACON_VOICE_MAX_CHARS` | `500` |
| `BEACON_DATE_FORMAT` | `%d-%m-%Y %H:%M` |
| `BEACON_FRAME_DESTINATION` | `NFO` |
| `BEACON_FRAME_PREFIX` / `BEACON_FRAME_SUFFIX` | `""` / `""` |
| `BEACON_AX25_KISS_HOST` / `_PORT` | `localhost` / `8001` |
| `BEACON_AX25_CONNECT_TIMEOUT_SECONDS` | `5` |
| `BEACON_VOICE_TRANSMITTER` | `logging` |
| `BEACON_TTS_ENGINE` | `piper` |
| `BEACON_TTS_VOICE` | `es` |
| `BEACON_TTS_PIPER_MODEL` | `storage/piper_voices/es_MX-claude-high.onnx` |
| `BEACON_TTS_PIPER_BINARY` | `piper` |
| `BEACON_TTS_WAV_DIR` | `storage/beacon_tts` |
| `BEACON_QUEUE_MAX_SIZE` | `200` |
| `BEACON_CONTENT_READY_RECONCILE_INTERVAL_SECONDS` | `30` |
| `BEACON_MQ_HOST`/`_PORT`/`_QOS`/`_RECONNECT_BACKOFF_SECONDS` | `localhost`/`1883`/`1`/`5` |
| `BEACON_CONTENT_READY_SUBSCRIBE_TOPIC` | `radiobeacon/events/item.content_ready` |
| `BEACON_NTP_SERVER` | `pool.ntp.org` |
| `BEACON_NTP_CHECK_INTERVAL_SECONDS` | `3600` |
| `BEACON_NTP_MAX_OFFSET_SECONDS` | `2.0` |
| `BEACON_SERVICE_CONTROLLER` | `logging` |
| `BEACON_SVXLINK_SERVICE_NAME` / `BEACON_DIREWOLF_SERVICE_NAME` | `svxlink` / `direwolf` |

## Tests

```bash
.venv/bin/pytest tests/ -v
```

Fully covered without any real hardware: `schedule.py` (pure timing,
injected time), the `beacon_tx_schedule` drain/retire logic (injected
`now_dt`), `formatters.py`, `content.py`, `kiss.py`
(a real fake TCP server stands in for Direwolf), the MQTT subscriber glue
(a fake `paho.mqtt` client, same convention as
[actions/tests/test_main.py](../actions/tests/test_main.py) and
[dispatcher/tests/test_mq_publisher.py](../dispatcher/tests/test_mq_publisher.py)),
`ntp.py`'s dispatch logic (mocked), `service_control.py`'s command
building (mocked `subprocess.run`), and the TDMA loop's helper functions.

Not testable here: `SvxlinkControlTransmitter`'s content-trigger (raises
by design), `synthesize_speech`'s actual audio correctness,
`kiss.py`'s byte-level correctness against a *real* TNC,
`SystemctlServiceController` against real systemd units, and real NTP
network reachability.
