# beacon

The TDMA transmission orchestrator for the CD3DXZ-1 experimental VHF
propagation beacon — the RF/transmission layer CONTEXT.md describes as
"designed but not implemented." This is that layer: it strictly queues
content arriving from the rest of the pipeline and delivers it to a voice
channel (SvxLink) or an AX.25 frame channel (Direwolf), inside a
configurable, repeating time window, since the two channels compete for
the same audio hardware and can't transmit simultaneously.

Not the same thing as `ui/src/ui/beacon.py` — that module is the "beacon
identity" settings page (`/beacon`'s callsign/description fields). No
Python import connects the two; they only share the DB.

## Content flow

```
dispatcher --item.dispatched--> actions.ai --item.ai_settled--> actions.chunk --item.chunked--\
                                                                                                 \-- actions.content_ready --item.content_ready--> beacon (frame + voice queues)
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
job on that event is therefore simple:

- **Frame**: one `QueuedFrame` per row in `chunks`, always — no
  special-casing for whether a summary existed, since `chunks` already
  reflects it.
- **Voice**: always one `QueuedVoice`, resolved via `items.summary` if
  present else `items.extracted_contents` at transmit time — the same
  summary-else-raw fallback `chunk` itself now uses, kept independently
  in `content.py` so CSN (whose short templated contents structurally
  never get summarized) stays voice-able regardless of whether AI is
  enabled.

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
audit trail, and enqueues anything genuinely missed. Confirmed live: on
one restart, `actions.content_ready` published a backlog of 52 items
faster than beacon's client finished connecting — all 52 would have been
silently lost without this.

Text resolution stays lazy (not baked in at enqueue time) for both: a
later rearm, or a summary changing between enqueue and transmit, is
naturally reflected — whatever's true right now is what gets sent.

Length limits are **not** new beacon-specific settings — they reuse
`ACTIONS_CHUNK_MAX_CHARS` (frame) and `ACTIONS_AI_MAX_CHARS` (voice, a
defensive ceiling only). `ACTIONS_CHUNK_MAX_CHARS` is itself a *ceiling*,
not a fixed size — `actions.chunk` dynamically clamps it down further at
runtime (`data-adapters/src/adapters/ax25.py`) against the current
`BEACON_CALLSIGN`/`BEACON_FRAME_DESTINATION`/`BEACON_FRAME_PREFIX`/
`BEACON_FRAME_SUFFIX`, so an assembled frame can't silently exceed
AX.25's ~256-byte limit and get dropped
(`beacon.frame.dropped_too_long`) regardless of how those change later.

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
`current_slot_remaining_seconds`, plus both queues' depth/dropped-total.
`ui/`'s `/beacon` page reads these to render a live cycle timeline and
slot countdown — see [ui/README.md](../ui/README.md#live-dashboard).

**A slot's length is a floor, not a hard ceiling.** When a slot opens,
`_run_tdma_loop` drains its *entire* queue — not one item — pausing
`BEACON_VOICE_INTER_TX_DELAY_SECONDS`/`BEACON_FRAME_INTER_TX_DELAY_SECONDS`
(default 2s each, a placeholder pending real-hardware measurement, same
caveat as `BEACON_SLOT_LEAD_TIME_SECONDS`) between consecutive
transmissions so PTT can release/re-key and the TNC/listener can clear
the previous one. If draining runs past the slot's nominal end, it
finishes the backlog before handing control back — nothing gets cut off
mid-queue. This blocks `_maybe_control_services`, the NTP check, and the
status heartbeat for the drain's duration; `BEACON_QUEUE_MAX_SIZE`
(default 200 — sized for frame content, where one queue slot is one
*chunk*, not one item; a real SENAPRED report has been observed
producing 57 chunks on its own) bounds the worst case to that many
sequential transmissions before control returns. Re-applied every tick
via `BoundedDropOldestQueue.set_maxsize` (`src/beacon/queues.py`), not
just read once at process start — a `/config` edit takes effect
immediately, same as every other beacon setting; shrinking it live drops
the oldest excess items right away rather than waiting for the next
`put()`. A deliberate tradeoff —
full-drain priority over strict timing — not an oversight.

## Enable / disable ("start/stop/restart")

Not real OS process control. `beacon` runs as a fifth long-lived process
(started by `bootstrap.sh`, alongside `data-adapters`/`dispatcher`/
`actions`/`ui`). The `/beacon` page's Enable/Disable button just flips the
`BEACON_ENABLED` settings flag, which the running process re-reads every
tick — same pattern `ACTIONS_AI_ENABLED` already uses elsewhere in this
repo. "Restart" means the next tick picks up whatever's in `settings`
now; nothing kills or respawns the actual process. Deliberate: this avoids
giving the still-auth-less UI the power to spawn/kill OS processes.
Content keeps being *enqueued* even while disabled (the "strict queue"
framing) — only the dequeue/transmit step checks the flag.

## Audio-device contention & service control

CONTEXT.md flags SvxLink/Direwolf fighting over the same audio hardware as
a "hallazgo crítico," with three candidate strategies, none chosen. This
package resolves it by actively stopping/starting each service around its
slot (`src/beacon/service_control.py`) — before a content slot begins
(with a configurable lead time, `BEACON_SLOT_LEAD_TIME_SECONDS`, standing
in for CONTEXT.md's own unmeasured `PTT_LATENCY_S`/`TNC_LATENCY_S`), and
only when there's actually something queued for it.

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

- **`espeak`** (default) — shells out to `espeak-ng` (offline, no API key,
  standard on Debian/Ubuntu — `apt install espeak-ng`). Zero setup, but
  sounds noticeably robotic (formant synthesis, not neural).
- **`piper`** — shells out to `piper` (offline neural TTS, still no API
  key). Sounds much more natural, but needs a voice model downloaded
  separately (`BEACON_TTS_PIPER_MODEL`, a `.onnx` file with its
  `.onnx.json` sidecar alongside it — see
  [piper's releases](https://github.com/rhasspy/piper/releases/tag/v0.0.2)
  for voices). Text is piped over stdin, matching piper's own CLI
  contract.

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
not content a listener decodes. Applied to every frame, including each
chunk of a multi-frame item (not just once per item), so a listener
catching only one frame still sees it; counts against the same 256-byte
`FrameTooLongError` ceiling as the rest of the frame (see
`formatters.py`). Both are str.format templates, not plain literals —
`{date}` (`items.source_date_time`, converted to `DISPLAY_TIMEZONE` and
formatted per `BEACON_DATE_FORMAT`) is the only placeholder currently
supported, resolved fresh at transmit time same as `text` itself.
`actions.chunk`'s dynamic `ACTIONS_CHUNK_MAX_CHARS` clamp
(`data-adapters/src/adapters/ax25.py`) accounts for `{date}`'s actual
*rendered* length, not the raw template's, so a short `" {date}"` suffix
can't silently under-clamp and cause an overflow at transmit time.

## Not addressed

Continuous, content-independent periodic station identification (CW ID)
— CONTEXT.md's regulatory section requires "mantener identificación en CW
según normativa del país," a third content channel (Morse code) distinct
from both voice and AX.25 frames. This package's queue-driven design
("only transmit when there's something queued") doesn't produce that on
its own — a real compliance gap worth a follow-up, not silently decided
here.

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
| `BEACON_DATE_FORMAT` | `%d-%m-%Y %H:%M` |
| `BEACON_FRAME_DESTINATION` | `WXALRT` |
| `BEACON_FRAME_PREFIX` / `BEACON_FRAME_SUFFIX` | `""` / `""` |
| `BEACON_AX25_KISS_HOST` / `_PORT` | `localhost` / `8001` |
| `BEACON_AX25_CONNECT_TIMEOUT_SECONDS` | `5` |
| `BEACON_VOICE_TRANSMITTER` | `logging` |
| `BEACON_TTS_ENGINE` | `espeak` |
| `BEACON_TTS_VOICE` | `es` |
| `BEACON_TTS_PIPER_MODEL` | `""` |
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
injected time), `queues.py`, `formatters.py`, `content.py`, `kiss.py`
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
