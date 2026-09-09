# beacon

The transmission layer for an experimental amateur-radio propagation beacon —
the RF layer CONTEXT.md describes as "designed but not implemented." This is
that layer: it schedules content arriving from the rest of the pipeline into a
durable transmit schedule, renders each unit to a **single WAV file**, and hands
that WAV to SvxLink (via the `svxlink-txqueue` spool folder — see
[../documentation/svxlink-txqueue-SETUP.md](../documentation/svxlink-txqueue-SETUP.md))
to be played on air when the RF channel is idle.

The operator picks **one beacon type** (`BEACON_TYPE`): `voice` (spoken-word TTS)
or `frame` (an AX.25 UI frame rendered to 1200-baud AFSK audio). There is no TDMA
schedule, no running Direwolf, and no sound-card contention to arbitrate —
SvxLink is the only thing that touches the radio. Each item is put on air
`transmit_count` times, `transmit_interval_seconds` apart (or one per
`transmit_cron` occurrence) — the schedule its `policy` names (see "Transmit schedule" below).

Not the same thing as `ui/src/ui/beacon.py` — that module is the "beacon
identity" settings (callsign/description fields under `/config`). No Python
import connects the two; they only share the DB.

## Content flow

```
dispatcher --item.dispatched--> actions.ai --item.ai_settled--> actions.chunk --item.chunked--\
                                                                                               \-- actions.content_ready --item.content_ready--> beacon (beacon_tx_schedule)
```

A linear pipeline, not a race: `actions.ai` always runs first (and always
publishes — see [actions/README.md](../actions/README.md)); `actions.chunk`
subscribes to `ai`'s output, so it always runs *after* `ai` has settled,
chunking `items.summary` when one exists and falling back to
`extracted_contents` otherwise. By the time `actions.content_ready` fires
`item.content_ready`, the `chunks` table already holds the best available
content, correctly sized. Beacon's job on that event is to insert (or reset, on
a rearm) rows into `beacon_tx_schedule` for the **configured `BEACON_TYPE`
only**, snapshotting the item's `policy` name:

- **`BEACON_TYPE=frame`**: one `kind="frame"` row per row in `chunks` (`ref` =
  `str(chunk_index)`).
- **`BEACON_TYPE=voice`**: one `kind="voice"` row (`ref = ""`), resolved via
  `items.summary` at transmit time.

Rows of the *other* kind are never scheduled, and any left over from a previous
`BEACON_TYPE` are cleared on startup and whenever the setting changes
(`delete_tx_schedule_other_kinds`).

**MQTT delivery alone isn't trusted as the sole path in.** beacon's MQTT client
uses `clean_session=True` (a persistent session's stale subscriptions caused
real double-transmission earlier), so a message published while beacon is
momentarily disconnected is gone at the MQTT layer.
`_reconcile_missed_content_ready` closes that gap: periodically
(`BEACON_CONTENT_READY_RECONCILE_INTERVAL_SECONDS`, default 30s — plus once
immediately on startup) it compares `actions.content_ready`'s own durable
publish record (`item_readiness`) against beacon's own
`beacon.content_ready.enqueued` audit trail and schedules anything genuinely
missed.

Text resolution stays lazy (not baked in at schedule time): a later rearm, or a
summary changing between schedule and transmit, is naturally reflected.

Frame's length limit reuses `ACTIONS_CHUNK_MAX_CHARS` — `actions.chunk`
dynamically clamps it against the current
`BEACON_CALLSIGN`/`BEACON_FRAME_DESTINATION`/`BEACON_FRAME_PREFIX`/`BEACON_FRAME_SUFFIX`
so an assembled frame can't silently exceed AX.25's ~256-byte limit and get
dropped (`beacon.frame.dropped_too_long`). Voice's limit is its own
`BEACON_VOICE_MAX_CHARS` — a time-budget cap, word-boundary truncated, no part
markers.

## The transmit loop

One process (`src/beacon/__main__.py`), two threads: an MQTT subscriber that
schedules rows, and `_run_transmit_loop` that drains them.

Every tick (`BEACON_TICK_SECONDS`, default 2s — but `_handle_content_ready_event`
sets a `wake_event` so newly scheduled content is picked up almost immediately):

1. re-read `BEACON_TYPE`, `BEACON_ENABLED`, and the rest of the per-tick config;
2. run the periodic NTP check and content-ready reconcile if due;
3. `_write_heartbeat` → `beacon_status` (`process_heartbeat_at`, `beacon_type`,
   and the pending row counts under the historical
   `voice_queue_depth`/`frame_queue_depth` keys);
4. if `BEACON_ENABLED`, `_drain_kind` for the configured type.

`_drain_kind` transmits *every* currently-due `beacon_tx_schedule` row of that
kind, pausing `BEACON_INTER_TX_DELAY_SECONDS` (default 2s) between clips so PTT /
the svxlink-txqueue channel-idle wait can settle. Due-ness and retirement are
computed live against each row's current `policy`.

`src/beacon/ntp.py` is a purely optional, read-only visibility layer — it
periodically checks the measured clock offset against a real NTP server for the
dashboard and never adjusts anything. The OS's own NTP daemon does actual clock
discipline.

## Transmit schedule (durable repeat)

`beacon_tx_schedule` (owned by
[`adapters.storage`](../data-adapters/src/adapters/storage.py)) is the source of
truth for what beacon still has to put on air:

```sql
CREATE TABLE beacon_tx_schedule (
    source              TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    kind                TEXT NOT NULL,             -- 'frame' | 'voice' (matches BEACON_TYPE)
    ref                 TEXT NOT NULL DEFAULT '',  -- frame = str(chunk_index); voice = ''
    policy             TEXT,                      -- Policy NAME snapshot
    sent_count          INTEGER NOT NULL DEFAULT 0,
    last_transmitted_at TEXT,                      -- NULL = never sent (due now)
    enqueued_event_id   TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, item_id, kind, ref)
);
```

- **Durable** — survives a beacon restart.
- **Live policy** — every cycle, `_drain_kind` resolves
  `adapters.policy.policy_for(row.policy)` fresh. Due iff
  `last_transmitted_at IS NULL` or
  `utc_now() >= last_transmitted_at + interval_seconds`. Retired (row deleted,
  `beacon.tx.retired` recorded) iff `sent_count >= transmit_count`.
- **Every attempt counts** — `sent_count` increments whether the render + spool
  hand-off succeeded or not, so a persistently failing link still retires the
  row.
- **Stale rows age out** — every tick (regardless of `BEACON_ENABLED`),
  `_purge_stale_rows` deletes any row whose `updated_at` (last enqueue / rearm /
  send) is older than `BEACON_MAX_QUEUED_AGE_SECONDS` (default `21600` = 6h,
  `0` disables), recording `beacon.tx.skipped_stale` per row. This is what keeps
  turning transmit off overnight from dumping a stale backlog on air the next
  morning. `beacon_manual_tx` is not affected.
- **Rearm / policy change** — a fresh `item.content_ready` (new CloudEvent id)
  PK-upserts each row back to `sent_count = 0`, `last_transmitted_at = NULL`.
- **Kind-generic** — `KIND_TRANSMITTERS` maps a kind to its
  `(conn, row, ctx) -> bool` transmitter; `_drain_kind` never special-cases.

## Enable / disable

Not real OS process control. `beacon` runs as a fifth long-lived process. The
dashboard's Enable/Disable button flips the `BEACON_ENABLED` settings flag, which
the running process re-reads every tick. Schedule rows keep being written even
while disabled — only the transmit step checks the flag. They don't pile up
forever, though: the staleness purge (`BEACON_MAX_QUEUED_AGE_SECONDS`, see
"Transmit schedule" above) runs while disabled too, so re-enabling after a long
off period replays only what's still fresh, not the whole overnight backlog.

## Manual transmission (one-shot)

`beacon_manual_tx` (also owned by `adapters.storage`) holds messages an operator
types into the dashboard's **Transmit now** action. Each row carries its own
literal `text` and a `kind` (`voice` | `frame`) — there's no item to resolve and
no `policy`. Every tick, `_drain_manual_tx` sends each row whose `kind`
matches the active `BEACON_TYPE` (voice wrapped by `BEACON_MANUAL_VOICE_TEMPLATE`
so the callsign is always spoken; frame straight through the AX.25 header), then
deletes it — a manual send is attempted exactly once, on air or not, and never
retried. Rows for the other kind wait until the operator switches mode. Gated by
`BEACON_ENABLED` like the schedule drain: composed while disabled, sent once
re-enabled. Audit events: `beacon.manual.enqueued` (written by the UI),
`beacon.manual.transmitted` / `.transmit_failed` / `.dropped_too_long` /
`.skipped_no_callsign`.

## Voice: TTS → WAV

`src/beacon/voice.py`'s `synthesize_speech` turns resolved voice text into a WAV,
via either of two selectable engines (`BEACON_TTS_ENGINE`):

- **`piper`** (default) — shells out to `piper` (`piper-tts`, in
  `requirements.txt`; offline neural TTS). Needs a `.onnx` voice model
  (`BEACON_TTS_PIPER_MODEL`, with its `.onnx.json` sidecar). The default model
  (`es_MX-claude-high`, ~60MB) is gitignored and downloaded by `start.sh` on
  first run via `ensure_default_piper_voice`.
- **`espeak`** — shells out to `espeak-ng` (`apt install espeak-ng`). Zero
  setup, robotic.

### Attention tone

`BEACON_VOICE_ATTENTION_TONE` prepends a short sequence of beeps to every
rendered voice WAV — scheduled bulletins **and** manual "Transmit now", but not
the watermark — so listeners recognize an announcement is starting. The value
is comma-separated `freq:ms` pairs, `freq` in Hz with `0` meaning silence: e.g.
`1400:250,0:120,1400:250` is two 250 ms tones 120 ms apart. Fresh installs get
a ~2 s rising/falling seven-note sweep
(`500:250,600:250,700:300,800:300,700:250,600:250,500:350`); clear the setting
to disable the tone. Generated by `adapters.attention_tone` (pure stdlib, shared with the
UI's config-page preview button) and mixed in by `prepend_tone_to_wav` after
TTS, following `frame_audio._prepend_silence`'s best-effort WAV-rewrite
pattern — a malformed spec is logged and skipped, never fatal to the
transmission.

## Frame: AX.25 → AFSK WAV

`src/beacon/frame_audio.py`'s `synthesize_frame_wav` renders the assembled TNC2
line (`CALLSIGN>DEST:content`, built by `formatters.format_frame`) to a
1200-baud AFSK WAV by shelling out to **Direwolf's `gen_packets` CLI**
(`apt install direwolf` — the binary is invoked one-shot per frame; **no
Direwolf process ever runs**, and there is no KISS/TCP connection). It emits
16 kHz mono to match SvxLink's internal rate, and prepends
`BEACON_FRAME_LEAD_SILENCE_MS` of silence so the first bits aren't clipped while
SvxLink keys the transmitter.

`BEACON_FRAME_PREFIX`/`BEACON_FRAME_SUFFIX` (empty by default) wrap the
transmitted payload — distinct from `BEACON_FRAME_DESTINATION`, which only
labels the AX.25 tocall. `BEACON_VOICE_PREFIX`/`BEACON_VOICE_SUFFIX` are the
equivalent for voice, wrapping the spoken text before it's substituted into
`BEACON_VOICE_TEMPLATE`'s `{text}`. Their defaults wrap the summary in a
spoken-Spanish informational-bulletin envelope (`Información de {source_name},
{date}. … Fin del comunicado.`), with `BEACON_VOICE_TEMPLATE` left as a bare
`{text}` passthrough; the prefix/suffix sit outside `BEACON_VOICE_MAX_CHARS`,
so the envelope is always spoken in full even when the summary is truncated.

All of these (plus `BEACON_VOICE_TEMPLATE`) are `str.format` templates. Beyond
`{date}` (`items.source_date_time`, converted to `DISPLAY_TIMEZONE`, formatted
per `BEACON_DATE_FORMAT`), each accepts `{source}`, `{item_id}`, `{type}`,
`{subtype}`, `{extracted_title}`, `{url}` (`content.resolve_item_fields`), and
per-*source* `{source_name}`/`{source_url}` (the `sources` table). Rendered via
`adapters.templating.safe_format` — an invalid placeholder logs and falls back
to `""` rather than raising.

## On-air hand-off

`src/beacon/transmit.py`:

- **`BEACON_WAV_TRANSMITTER=logging`** (default) — logs the WAV it would hand
  off. The only path verifiable without a spool folder / SvxLink.
- **`BEACON_WAV_TRANSMITTER=spool`** — copies the WAV (write-then-rename) into
  `BEACON_TXQUEUE_INCOMING_DIR` (default `/var/spool/svxlink-tx/incoming`, must
  match `svxlink-txqueue`'s `TXQUEUE_SPOOL/incoming` — its documented drop
  point). `svxlink-txqueue` stamps a FIFO timestamp, converts to 16 kHz mono,
  waits for the RF channel to be idle, then triggers SvxLink to play the clip.

**Deploying SvxLink and the `svxlink-txqueue` service itself is out of scope for
this package** — see
[../documentation/svxlink-txqueue-SETUP.md](../documentation/svxlink-txqueue-SETUP.md).

The dashboard groups the beacon-side knobs under `/config` → **Beacon — SvxLink**
(`BEACON_WAV_TRANSMITTER`, `BEACON_TXQUEUE_INCOMING_DIR`) and **Beacon — Direwolf**
(`BEACON_GEN_PACKETS_BINARY`, `BEACON_FRAME_LEAD_SILENCE_MS`). Those two pages
also carry an **optional raw editor for the real `svxlink.conf` / `direwolf.conf`**
on the host — OFF by default, gated on `BEACON_RF_CONF_EDITOR_ENABLED`. The
dashboard has no login, so with it on anyone who can reach the UI can overwrite
any file the UI process can write; only enable it on a trusted host. A saved
edit writes a timestamped `.bak` and never restarts anything — the page shows the
`systemctl restart …` command to run.

## Not addressed

Continuous, content-independent periodic station identification (CW ID) —
CONTEXT.md's regulatory section requires "mantener identificación en CW." Now
that everything is a WAV in one spool folder, this becomes straightforward to
add later: render a Morse WAV (`gen_packets -M`) on a timer straight into the
incoming folder. Not built here.

## Setup

```bash
./start.sh
```

Creates `.venv`, installs `requirements.txt`, loads `../.env`, `exec`s into
`PYTHONPATH=src python3 -m beacon`. For `BEACON_TYPE=frame` you also need
`gen_packets` on `PATH` (`apt install direwolf`).

## Configuration

Env vars in `.env` at the repo root — also all editable live via `/config` → the
"Beacon — *" groups, except the beacon identity fields (DB-only).

| var | default |
|---|---|
| `BEACON_ENABLED` | `false` |
| `BEACON_TYPE` | `voice` (`voice` \| `frame`) |
| `BEACON_TICK_SECONDS` | `2` |
| `BEACON_INTER_TX_DELAY_SECONDS` | `2` |
| `BEACON_WAV_TRANSMITTER` | `logging` (`logging` \| `spool`) |
| `BEACON_TXQUEUE_INCOMING_DIR` | `/var/spool/svxlink-tx/incoming` |
| `BEACON_SVXLINK_CONF_PATH` | `/etc/svxlink/svxlink.conf` (editor only; blank disables) |
| `BEACON_DIREWOLF_CONF_PATH` | `""` (editor only; blank hides) |
| `BEACON_RF_CONF_EDITOR_ENABLED` | `false` (dashboard editor for the two files above) |
| `BEACON_TX_MONITOR_ENABLED` | `true` (tail the SvxLink log for key-up/key-down → dashboard "ON AIR" glow; read-only, self-disabling if the log isn't readable) |
| `BEACON_SVXLINK_LOG_PATH` | `/var/log/svxlink` (log the ON AIR monitor tails; beacon user needs read access — e.g. the `svxlink`/`adm` group; blank disables) |

While the ON AIR monitor is reporting, the dashboard can also play a live stream of
SvxLink's transmit audio in the browser — set the UI's `UI_DASHBOARD_TX_STREAM_URL`
(see [ui/README.md](../ui/README.md#hear-the-transmission-live) and
[documentation/svxlink-txqueue-SETUP.md](../documentation/svxlink-txqueue-SETUP.md)).
| `BEACON_VOICE_TEMPLATE` | `{text}` |
| `BEACON_VOICE_PREFIX` | `Información de {source_name}, {date}. ` |
| `BEACON_VOICE_SUFFIX` | `. Para más información consulte fuentes oficiales de {source_name}. Fin del comunicado. ` |
| `BEACON_VOICE_MAX_CHARS` | `750` |
| `BEACON_VOICE_ATTENTION_TONE` | `500:250,600:250,700:300,800:300,700:250,600:250,500:350` (comma-separated `freq:ms` pairs, `0` Hz = silence; clear to disable) |
| `BEACON_DATE_FORMAT` | `%d-%m-%Y %H:%M` |
| `BEACON_FRAME_DESTINATION` | `NFO` |
| `BEACON_FRAME_PREFIX` / `BEACON_FRAME_SUFFIX` | `""` / `""` |
| `BEACON_GEN_PACKETS_BINARY` | `gen_packets` |
| `BEACON_FRAME_LEAD_SILENCE_MS` | `250` |
| `BEACON_TTS_ENGINE` | `piper` |
| `BEACON_TTS_VOICE` | `es` |
| `BEACON_TTS_PIPER_MODEL` | `storage/piper_voices/es_MX-claude-high.onnx` |
| `BEACON_TTS_PIPER_BINARY` | `piper` |
| `BEACON_TTS_WAV_DIR` | `storage/beacon_tts` (a relative path is resolved against the repo root, not `beacon/`, so the rendered clips land in the top-level `storage/` the dashboard also reads) |
| `BEACON_MANUAL_VOICE_TEMPLATE` | `Aquí {callsign}. {text}` |
| `BEACON_QUEUE_MAX_SIZE` | `200` |
| `BEACON_MAX_QUEUED_AGE_SECONDS` | `21600` (`0` disables) |
| `BEACON_CONTENT_READY_RECONCILE_INTERVAL_SECONDS` | `30` |
| `BEACON_MQ_HOST`/`_PORT`/`_QOS`/`_RECONNECT_BACKOFF_SECONDS` | `localhost`/`1883`/`1`/`5` |
| `BEACON_CONTENT_READY_SUBSCRIBE_TOPIC` | `radiobeacon/events/item.content_ready` |
| `BEACON_NTP_SERVER` | `pool.ntp.org` |
| `BEACON_NTP_CHECK_INTERVAL_SECONDS` | `3600` |
| `BEACON_NTP_MAX_OFFSET_SECONDS` | `2.0` |
| `LOG_LEVEL` | `INFO` (`DEBUG` \| `INFO` \| `WARNING` \| `ERROR`; shared by every service, re-read live — `/config → Logging`) |

## Tests

```bash
.venv/bin/pytest tests/ -v
```

Fully covered without any real hardware: the `beacon_tx_schedule` drain/retire
logic (injected `now_dt`), `BEACON_TYPE`-driven scheduling, `formatters.py`,
`content.py`, `transmit.py` (real temp-dir spool), `frame_audio.py` (mocked
`gen_packets`), the MQTT subscriber glue (a fake `paho.mqtt` client), and
`ntp.py`'s dispatch logic (mocked).

Not testable here: `synthesize_speech` / `gen_packets` actual audio correctness,
the real `svxlink-txqueue` hand-off, and real NTP network reachability.
