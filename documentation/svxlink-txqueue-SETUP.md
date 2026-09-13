# svxlink-txqueue — transmit pre-recorded WAV files over SvxLink

A small service that watches a folder, queues any `*.wav` dropped into it, and
plays each one on the air through an existing SvxLink logic — one at a time, only
when the RF channel is idle.

- **No repeater conversion, no extra receiver, no sound-card tricks.**
- Works on a stock `TYPE=Simplex` (or `Repeater`) logic.
- Pure Python 3 standard library — no `ffmpeg`, `sox`, or `audioop` needed.

Tested on SvxLink `1.9.1@25.05.1`, Python 3.14, Ubuntu-family systemd.

---

## 1. How it works

SvxLink logics expose an optional **`COMMAND_PTY`** — a pseudo-tty the OS can
write commands to. One of its commands is:

```
EVENT <tcl-proc> [args...]
```

which calls a TCL event handler in the logic. The built-in `::playFile` handler
keys the transmitter, plays an audio file through the logic's message handler,
and unkeys — exactly what SvxLink itself does for hourly voice IDs.

So the whole feature is:

```
incoming/*.wav ──► queue/ ──► convert to 16 kHz mono WAV ──► staging/
                                        │
                     wait for idle channel
                                        │
        echo 'EVENT ::playFile /…/staging/clip.wav' > $COMMAND_PTY
                                        │
             SvxLink keys TX, plays clip, unkeys
                                        │
                          move original ──► sent/
```

### Why not "inject it as received audio"?

A `TYPE=Simplex` logic **never retransmits received audio**. Feeding a WAV into a
fake receiver (UDP audio device, ALSA loopback, etc.) opens the squelch but the
transmitter stays off. Only ident / DTMF replies / modules / **TCL-queued
announcements** produce transmit audio on a simplex logic. `COMMAND_PTY` +
`EVENT ::playFile` is the announcement path, driven from outside.

A `TYPE=Repeater` logic *does* repeat RX audio, but you still don't want to fake
a receiver just for announcements — `COMMAND_PTY` works there too, identically.

### Where a digital (frame) bulletin fits in

Not every clip that lands in `incoming/` is speech. In `BEACON_TYPE=frame`
mode, radiobeacon's own `beacon` process renders an AX.25 UI frame to an AFSK
WAV by calling Direwolf's `gen_packets` **directly, as a one-shot subprocess**
— no Direwolf daemon runs, and Direwolf never touches `COMMAND_PTY` or TCL at
all. That WAV is dropped into the same `incoming/` folder a voice clip would
use:

```
  voice WAV (TTS)          ─┐
                             ├─► incoming/*.wav ──► queue/ ──► … (as above)
  frame WAV (gen_packets)  ─┘
```

Once staged, a frame WAV is indistinguishable from a voice WAV to this
daemon — both go through the exact same `EVENT ::playFile` hand-off. See
[2. Installing SvxLink and Direwolf](#2-installing-svxlink-and-direwolf-debianubuntu)
for getting `gen_packets` in place, and `beacon/README.md`'s "Frame: AX.25 →
AFSK WAV" section for how the frame itself is built.

> **Prefer to automate this?**
> [`svxlink-txqueue-install.sh`](svxlink-txqueue-install.sh) automates
> [2. Installing SvxLink and Direwolf](#2-installing-svxlink-and-direwolf-debianubuntu)
> and [4. Install](#4-install) below — packages, `svxlink.conf`, spool dirs,
> the daemon, the systemd unit, and (optionally) wiring radiobeacon's own
> `.env`. It stays scoped to exactly what's documented here: radio
> programming, the antenna/feedline, and your licence are still on you.
> `sudo ./documentation/svxlink-txqueue-install.sh --help` for every flag, or
> `--dry-run` to preview with no changes made.

---

## 2. Installing SvxLink and Direwolf (Debian/Ubuntu)

Starting from a bare Debian/Ubuntu box. If SvxLink is already installed and
running, skip to [3. Prerequisites](#3-prerequisites).

### 2.1 Install the packages

```sh
sudo apt update
sudo apt install svxlink-server direwolf alsa-utils
```

`svxlink-server` is the daemon (`svxlink` is a separate client package —
not needed here). `alsa-utils` provides `arecord`/`aplay`, used next.
`direwolf` is only needed for its `gen_packets` binary — see 2.5.

### 2.2 Find the radio interface's sound device

```sh
arecord -l   # capture (RX) side
aplay -l     # playback (TX) side
```

Look for the radio interface — a combined USB sound-card-and-PTT device such
as the **R1 2023** described in the main README's
["The radio interface"](../README.md#the-radio-interface) section. Note its
card number; SvxLink refers to it as `plughw:<card>,<device>`.

### 2.3 Write a minimal `svxlink.conf`

Edit `/etc/svxlink/svxlink.conf` with a bare `[SimplexLogic]` — audio and PTT
only, **no `COMMAND_PTY` yet**:

```ini
[SimplexLogic]
TYPE=Simplex
RX=Rx1
TX=Tx1
CALLSIGN=NOCALL

[Rx1]
TYPE=Local
AUDIO_DEV=alsa:plughw:1,0     # card number from 2.2
AUDIO_CHANNEL=0
SQL_DET=VOX
VOX_LIMIT=1000

[Tx1]
TYPE=Local
AUDIO_DEV=alsa:plughw:1,0     # same card as Rx1 on a combined interface
AUDIO_CHANNEL=0
PTT_TYPE=GPIO
PTT_PORT=/dev/hidraw0         # a CM108-style USB sound fob's GPIO PTT
PTT_PIN=GPIO3
```

`PTT_TYPE=GPIO` on a USB sound chip's GPIO pins is the common case for a
combined interface like the R1 2023; SvxLink also supports PTT over a serial
port's RTS/DTR lines or a CAT command — see
[SvxLink's own documentation](https://github.com/sm0svx/svxlink) for those,
they're not re-explained here.

This step proves the audio/PTT chain works on its own — a bare logic that can
key up and play its own station ID. `COMMAND_PTY` isn't part of it yet: that
one line gets added to this exact block in
[4.1 SvxLink config](#41-svxlink-config), once this base is confirmed working.
That's the point where the TCL Event mechanism from
[1. How it works](#1-how-it-works) goes from "how it works" to "working on
this box."

### 2.4 Enable, start, and smoke-test SvxLink

```sh
sudo systemctl enable --now svxlink
journalctl -u svxlink -f
```

Confirm the logic loads with no errors and the station identifies on its own
schedule. This is the checkpoint before touching `svxlink-txqueue` at all.

### 2.5 Confirm Direwolf, and stop there

No service, no `direwolf.conf`, nothing to enable:

```sh
which gen_packets
gen_packets --help
```

`gen_packets` is invoked directly by radiobeacon's own
`beacon/frame_audio.py`, one-shot per frame — see "Where a digital (frame)
bulletin fits in" above. Relevant config on radiobeacon's side:
`BEACON_GEN_PACKETS_BINARY` (the command name/path) and
`BEACON_DIREWOLF_CONF_PATH` (blank by default — only used if you separately
run a full Direwolf instance for something else on this host), both under
`/config` → **Beacon — Direwolf** — see `beacon/README.md`.

---

## 3. Prerequisites

- SvxLink installed and running with at least one logic that has a **transmitter**
  (`TX=...`) — see [2. Installing SvxLink and Direwolf](#2-installing-svxlink-and-direwolf-debianubuntu)
  if you haven't done this yet. This guide assumes the logic section is called
  `[SimplexLogic]` — adjust the name everywhere if yours differs.
- The SvxLink service runs as a dedicated user (commonly `svxlink`). Find it:
  ```sh
  grep ^RUNASUSER= /etc/default/svxlink
  ```
  The Debian/Ubuntu `svxlink-server` package's unit has no systemd `User=` —
  it runs `svxlink --runasuser=${RUNASUSER}` via
  `EnvironmentFile=/etc/default/svxlink`, so `systemctl show svxlink -p User
  --value` returns empty rather than the real user; read `RUNASUSER` directly
  instead. Everything below uses `svxlink` — substitute your value.
- `python3` ≥ 3.8.
- Write access for the SvxLink user to the directory where the `COMMAND_PTY`
  symlink is created (default `/dev/shm`, which is world-writable — fine).

---

## 4. Install

### 4.1 SvxLink config

This is the one line that turns [2.3](#23-write-a-minimal-svxlinkconf)'s bare,
working logic into one `svxlink-txqueue` can drive. Edit
`/etc/svxlink/svxlink.conf`, in the `[SimplexLogic]` section — same block as
2.3, with `COMMAND_PTY` added and nothing else touched:

```ini
[SimplexLogic]
TYPE=Simplex
RX=Rx1
TX=Tx1
CALLSIGN=NOCALL
# Control PTY: svxlink-txqueue writes "EVENT ::playFile <abs-wav>" here to key
# the transmitter and play a pre-recorded clip.
COMMAND_PTY=/dev/shm/svxlink_simplex_ctrl
```

`[Rx1]`/`[Tx1]` stay exactly as configured in 2.3 — nothing else in
`svxlink.conf` changes. Note the logic's `TIMEOUT` value (default
`300` seconds) — it caps how long a single transmission may last, so it caps clip
length. The service refuses clips longer than `TIMEOUT − 5 s`.

> If the radiobeacon dashboard runs on this host, this one-line edit can also be
> made from **/config → Beacon — SvxLink** (set `BEACON_RF_CONF_EDITOR_ENABLED`
> on first — it is off by default because the dashboard has no login). It writes
> a timestamped `.bak` and does not restart SvxLink; run `systemctl restart
> svxlink` yourself afterward.

Restart SvxLink and confirm the PTY appears:

```sh
systemctl restart svxlink
ls -l /dev/shm/svxlink_simplex_ctrl        # -> symlink to /dev/pts/N, owned by the svxlink user
```

### 4.2 Spool directories

```sh
for d in incoming queue staging sent failed; do
    install -d -o svxlink -g svxlink -m 2775 "/var/spool/svxlink-tx/$d"
done
```

`incoming/` is `2775` (group-writable, setgid) so operators in the `svxlink`
group can drop files over SFTP without `sudo`. Add a user to that group with:

```sh
usermod -aG svxlink <username>
```

### 4.3 The daemon

Save as `/usr/local/bin/svxlink-txqueue`, then `chmod +x` it. Full script in
[Appendix A](#appendix-a--usrlocalbinsvxlink-txqueue).

```sh
install -m 0755 svxlink-txqueue /usr/local/bin/svxlink-txqueue
python3 -c "import ast; ast.parse(open('/usr/local/bin/svxlink-txqueue').read())" && echo OK
```

### 4.4 The systemd unit

Save as `/etc/systemd/system/svxlink-txqueue.service` (full text in
[Appendix B](#appendix-b--etcsystemdsystemsvxlink-txqueueservice)), then:

```sh
systemctl daemon-reload
systemctl enable --now svxlink-txqueue.service
systemctl status svxlink-txqueue.service
```

---

## 5. Usage

Drop a WAV into the watched folder:

```sh
cp announcement.wav /var/spool/svxlink-tx/incoming/
```

- Any PCM WAV is accepted: 8/16/24/32-bit, mono or stereo, any sample rate. It is
  down-mixed to mono and resampled to 16 kHz (SvxLink's internal rate).
- The file is picked up once it stops growing (2 s of no change), queued with a
  timestamp prefix (so processing is strict FIFO), transmitted when the channel
  is idle, then moved to `sent/`.
- On any error the file goes to `failed/` with a `<name>.log` sidecar containing
  the reason / traceback.

Watch it work:

```sh
journalctl -fu svxlink-txqueue
tail -f /var/log/svxlink        # in another terminal
```

### Tunables (systemd `Environment=` or a drop-in)

| Variable | Default | Meaning |
|---|---|---|
| `TXQUEUE_SPOOL` | `/var/spool/svxlink-tx` | spool root |
| `TXQUEUE_COMMAND_PTY` | `/dev/shm/svxlink_simplex_ctrl` | must match `svxlink.conf` |
| `TXQUEUE_SVXLINK_LOG` | `/var/log/svxlink` | log tailed for channel state |
| `TXQUEUE_TARGET_RATE` | `16000` | resample target (SvxLink internal rate) |
| `TXQUEUE_STABLE_SECONDS` | `2.0` | quiet time before a file is queued |
| `TXQUEUE_IDLE_GUARD` | `3.0` | channel must be idle this long before TX |
| `TXQUEUE_IDLE_MAX_WAIT` | `600` | give up waiting for idle, TX anyway |
| `TXQUEUE_TX_ON_TIMEOUT` | `20` | fail if TX doesn't key within this after trigger |
| `TXQUEUE_TX_OFF_MARGIN` | `15` | slack added to clip duration when waiting for TX-off |
| `TXQUEUE_SVX_TX_TIMEOUT` | `295` | reject clips longer than this (keep < logic `TIMEOUT`) |

Example drop-in:

```sh
mkdir -p /etc/systemd/system/svxlink-txqueue.service.d
cat > /etc/systemd/system/svxlink-txqueue.service.d/local.conf <<'EOF'
[Service]
Environment=TXQUEUE_IDLE_GUARD=5
Environment=TXQUEUE_COMMAND_PTY=/dev/shm/my_logic_ctrl
EOF
systemctl daemon-reload && systemctl restart svxlink-txqueue
```

---

## 6. Verification

> Steps 3–5 key the real transmitter. Use a dummy load or a clear simplex
> frequency, and identify per your licence.

1. **Config loads** — `systemctl restart svxlink`; `/var/log/svxlink` shows the
   logic and its RX/TX loading with no errors; `ls -l /dev/shm/svxlink_simplex_ctrl`
   is a symlink.
2. **Daemon starts** — `systemctl restart svxlink-txqueue`; journal shows
   `svxlink-txqueue started (... command_pty=... rate=16000)`; `staging/` exists.
3. **Manual PTY test** (run as the SvxLink user — even root gets `EACCES` on
   another user's pts slave):
   ```sh
   sudo -u svxlink sh -c \
     "printf 'EVENT ::playFile /usr/share/svxlink/sounds/en_US/Core/online.wav\n' > /dev/shm/svxlink_simplex_ctrl"
   ```
   `/var/log/svxlink` → `Turning the transmitter ON` … `OFF`, audio on air.
4. **Full path** — `cp test.wav /var/spool/svxlink-tx/incoming/`; journal shows
   `queued → transmitting (N.Ns) → done`; `Tx1: Turning the transmitter ON/OFF`
   in the SvxLink log; file lands in `sent/`; `staging/` is empty again.
5. **Failure path** — `printf 'not audio' > /var/spool/svxlink-tx/incoming/bad.wav`
   → ends up in `failed/` with a `.log` sidecar.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `gen_packets: command not found`, or `which gen_packets` prints nothing | Direwolf isn't installed or `gen_packets` isn't on `PATH` — `sudo apt install direwolf`; confirm with `dpkg -L direwolf \| grep bin`. Only matters for `BEACON_TYPE=frame`. |
| SvxLink and a separately-run Direwolf both fail with `Device or resource busy` on the same sound card | Two audio processes can't open the same ALSA device at once. This project only calls Direwolf's `gen_packets` one-shot (no daemon — see [2.5](#25-confirm-direwolf-and-stop-there)), so it never competes with SvxLink for the card by itself; this only bites if you *also* run a full, separate Direwolf instance on the same host for something unrelated. Use ALSA `dmix`/`dsnoop`, or don't run both against the same device at once. |
| File queued, `transmitter never keyed within Ns` → `failed/` | `COMMAND_PTY` name mismatch between `svxlink.conf` and the service env; or SvxLink not running; or `::playFile` got a bad path — check `/var/log/svxlink` for `*** ERROR`. |
| `<pty> missing - svxlink not running` | SvxLink down, or `COMMAND_PTY` not set in the logic section, or set on the wrong logic. |
| `write to <pty> failed: [Errno 5/6]` | Stale symlink after a SvxLink crash — `systemctl restart svxlink` recreates it. |
| Audio truncated | Clip longer than the logic `TIMEOUT`; SvxLink cut the transmission. Lower the clip length or raise `TIMEOUT`. |
| Nothing transmits, `idle wait timed out` never appears, journal quiet | Channel never goes idle (stuck squelch). Check the receiver; the service waits up to `TXQUEUE_IDLE_MAX_WAIT` then transmits anyway. |
| Garbled / wrong-speed audio | Source WAV is float or ADPCM, not PCM — `wave` only reads PCM. Re-export as PCM WAV. |
| Permission denied writing to `incoming/` over SFTP | Add the operator to the `svxlink` group and re-login. |
| `EVENT` path with spaces fails | The PTY parser splits on whitespace and ignores quoting. The service already sanitizes staged names to `[A-Za-z0-9._-]`; only relevant if you call the PTY by hand. |

### Behaviour notes

- A played clip counts as a logic announcement: the roger beep plays afterwards
  and identification timers reset, same as any other announcement.
- `MUTE_TX_ON_RX` (on by default) makes SvxLink itself defer the announcement if
  the channel opens between the trigger and TX; the service's idle-wait normally
  prevents that from mattering.
- Completion is detected by tailing `/var/log/svxlink` for
  `Turning the transmitter ON` then a stable `OFF`, bounded by
  `clip duration + TX_OFF_MARGIN`. If an ident happens to key TX around the same
  time, worst case is one extra ident cycle of waiting — the clip is never cut.

### Dashboard "ON AIR" indicator (optional, cosmetic)

radiobeacon's `beacon` process can show a live **ON AIR** glow on the operator
dashboard for as long as SvxLink actually holds the transmitter keyed. It does
this the same way this service tracks channel state — tailing `/var/log/svxlink`
for `Turning the transmitter (ON|OFF)` — and writes the result to `beacon_status`
for the UI to render.

- It is **read-only** and never touches SvxLink, the `COMMAND_PTY`, or
  transmission. Disabling or breaking it changes nothing except the indicator.
- It needs read access to the log. The `beacon` process usually doesn't run as
  the `svxlink` user, so add its user to the `svxlink` (or `adm`) group, or point
  `BEACON_SVXLINK_LOG_PATH` at a world-readable copy.
- If the log isn't present or readable — SvxLink on another host, in a container,
  or not set up yet — the monitor stays dormant and the dashboard simply shows
  nothing. Transmission is unaffected.
- Toggle with `BEACON_TX_MONITOR_ENABLED` / `BEACON_SVXLINK_LOG_PATH` (`.env` or
  `/config` → Beacon — SvxLink).

### Dashboard live transmission audio (optional)

While the rig is keyed the dashboard can also **play a live stream of SvxLink's
transmit audio** in the browser, with a per-viewer mute toggle. Set
`UI_DASHBOARD_TX_STREAM_URL` (`/config` → UI) to an HTTP(S) audio stream; the UI
reverse-proxies it at `/dashboard/tx-stream` and fans one upstream connection out
to every open dashboard, so bind the stream to `127.0.0.1`. Blank = feature off
(glow indicator still works).

**1. Capture what SvxLink plays out.** SvxLink's TX audio device is
playback-only, so route a copy somewhere capturable:

- *PulseAudio / PipeWire*: capture the output sink's `.monitor` source directly —
  `pactl list short sources | grep monitor`.
- *plain ALSA*: load `snd-aloop`, point SvxLink's `Tx` at `hw:Loopback,0,0`, bridge
  `hw:Loopback,1,0 → plughw:0,0` (the real USB card) with `alsaloop`, and capture
  `hw:Loopback,1,1`. (CONTEXT.md has the `plughw:0,0` / `dsnoop` background.)

**2. Serve it as HTTP.** Simplest, single source, no extra daemon —
`ffmpeg`'s built-in listener:

```sh
ffmpeg -f pulse -i "<sink>.monitor" \
  -ac 1 -c:a libmp3lame -b:a 48k \
  -f mp3 -content_type audio/mpeg -listen 1 http://127.0.0.1:8123/svxlink.mp3
```

`-listen 1` accepts a single client — the UI's fan-out hub *is* that one client,
serving every viewer from it. Then:

```
UI_DASHBOARD_TX_STREAM_URL=http://127.0.0.1:8123/svxlink.mp3
```

`ffmpeg -listen 1` **exits when that client disconnects** (the hub drops the
upstream ~10 s after the last dashboard viewer leaves), so run it from a `systemd`
unit with `Restart=always` (and `After=svxlink.service`); the dashboard reconnects
within a few seconds once it's back. If you run the UI with multiple uvicorn
workers, or want one always-warm encoder, put **Icecast** in front instead (each
worker is then its own Icecast listener).

**3.** The stream lags on-air detection by a couple of seconds (monitor poll +
stream buffer), so the first moments of a transmission are clipped — it's a
"is it working / does it sound right" monitor, not a recording.

---

## 8. Uninstall

Or run `sudo ./documentation/svxlink-txqueue-install.sh --uninstall` (add
`--purge-packages` to also remove the apt packages) — it reverses the same
steps below. Manually:

```sh
systemctl disable --now svxlink-txqueue.service
rm /etc/systemd/system/svxlink-txqueue.service
rm -r /etc/systemd/system/svxlink-txqueue.service.d      # if created
systemctl daemon-reload
rm /usr/local/bin/svxlink-txqueue
# remove 'COMMAND_PTY=...' from [SimplexLogic] in /etc/svxlink/svxlink.conf, then:
systemctl restart svxlink
rm -r /var/spool/svxlink-tx                              # optional: drops the spool
sudo apt remove svxlink-server direwolf                  # optional: drops the packages from 2.1
```

---

## Appendix A — `/usr/local/bin/svxlink-txqueue`

The canonical copy also lives at
[`svxlink-txqueue/svxlink-txqueue`](svxlink-txqueue/svxlink-txqueue) —
`svxlink-txqueue-install.sh` deploys that file directly. Kept in sync with
the listing below by hand; if you edit one, edit both.

```python
#!/usr/bin/python3
"""
svxlink-txqueue - transmit pre-recorded WAV files over SvxLink.

Drop a *.wav file into  /var/spool/svxlink-tx/incoming/  and this service will:

  1. wait until the file stops growing, then move it to  queue/
  2. process the queue strictly one file at a time, oldest first
  3. convert it to SvxLink's internal format (16 kHz / 16-bit / mono WAV)
     and stage it under  staging/  with a space-free name
  4. wait until the RF channel is idle (no open squelch, transmitter off)
  5. tell the SimplexLogic to play it:
        echo 'EVENT ::playFile <staged-wav>' > $COMMAND_PTY
     SvxLink keys Tx1, plays the clip through its message handler, unkeys
  6. move the original file to  sent/  (or  failed/ on error, with a .log sidecar)

Audio is converted in pure Python (no ffmpeg/sox): any PCM WAV (8/16/24/32-bit,
mono or stereo, any sample rate) is accepted, down-mixed to mono and resampled.

Matching SvxLink config: /etc/svxlink/svxlink.conf -> [SimplexLogic] COMMAND_PTY.
"""

from __future__ import annotations

import argparse
import array
import os
import re
import signal
import sys
import time
import traceback
import wave
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration (override via environment)
# ---------------------------------------------------------------------------
SPOOL        = Path(os.environ.get("TXQUEUE_SPOOL", "/var/spool/svxlink-tx"))
INCOMING     = SPOOL / "incoming"
QUEUE        = SPOOL / "queue"
STAGING      = SPOOL / "staging"
SENT         = SPOOL / "sent"
FAILED       = SPOOL / "failed"

COMMAND_PTY  = os.environ.get("TXQUEUE_COMMAND_PTY", "/dev/shm/svxlink_simplex_ctrl")
SVXLINK_LOG  = os.environ.get("TXQUEUE_SVXLINK_LOG", "/var/log/svxlink")
TARGET_RATE  = int(os.environ.get("TXQUEUE_TARGET_RATE", "16000"))   # SvxLink internal rate

STABLE_S        = float(os.environ.get("TXQUEUE_STABLE_SECONDS", "2.0"))  # incoming file quiet this long
IDLE_GUARD_S    = float(os.environ.get("TXQUEUE_IDLE_GUARD", "3.0"))      # channel idle this long
IDLE_MAX_S      = float(os.environ.get("TXQUEUE_IDLE_MAX_WAIT", "600"))   # ... but wait at most this long
TX_ON_TIMEOUT_S = float(os.environ.get("TXQUEUE_TX_ON_TIMEOUT", "20"))    # wait for TX to key after trigger
TX_OFF_MARGIN_S = float(os.environ.get("TXQUEUE_TX_OFF_MARGIN", "15"))    # slack on top of clip duration
SVX_TX_TIMEOUT_S = float(os.environ.get("TXQUEUE_SVX_TX_TIMEOUT", "295")) # < [SimplexLogic] TIMEOUT=300
POLL_S       = 0.5

_stop = False


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    log(f"received signal {signum}, shutting down after current file")


# ---------------------------------------------------------------------------
# WAV loading / conversion
# ---------------------------------------------------------------------------
def _frames_to_int16_mono(raw: bytes, width: int, channels: int) -> array.array:
    """Decode interleaved PCM frames -> mono signed-16 samples (average of channels)."""
    if width == 2:
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder == "big":
            samples.byteswap()
    elif width == 1:
        # WAV 8-bit is unsigned, midpoint 128
        samples = array.array("h", ((b - 128) << 8 for b in raw))
    elif width == 3:
        n = len(raw) // 3
        samples = array.array("h", [0]) * n
        for i in range(n):
            b0, b1, b2 = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
            v = b0 | (b1 << 8) | (b2 << 16)
            if v & 0x800000:
                v -= 0x1000000
            samples[i] = v >> 8
    elif width == 4:
        ints = array.array("i")
        ints.frombytes(raw)
        if sys.byteorder == "big":
            ints.byteswap()
        samples = array.array("h", (v >> 16 for v in ints))
    else:
        raise ValueError(f"unsupported sample width: {width} bytes")

    if channels <= 1:
        return samples

    # down-mix to mono
    mono = array.array("h", [0]) * (len(samples) // channels)
    for i in range(len(mono)):
        acc = 0
        base = i * channels
        for c in range(channels):
            acc += samples[base + c]
        mono[i] = max(-32768, min(32767, acc // channels))
    return mono


def _resample_linear(mono: array.array, src_rate: int, dst_rate: int) -> array.array:
    if src_rate == dst_rate or len(mono) == 0:
        return mono
    n_src = len(mono)
    n_dst = max(1, int(n_src * dst_rate / src_rate))
    out = array.array("h", [0]) * n_dst
    step = src_rate / dst_rate
    pos = 0.0
    last = n_src - 1
    for i in range(n_dst):
        j = int(pos)
        if j >= last:
            out[i] = mono[last]
        else:
            frac = pos - j
            out[i] = int(mono[j] * (1.0 - frac) + mono[j + 1] * frac)
        pos += step
    return out


_safe_re = re.compile(r"[^A-Za-z0-9._-]+")


def stage_wav(src: Path) -> tuple[Path, float]:
    """Convert src to a 16 kHz / 16-bit / mono WAV under STAGING/. Return (path, seconds).

    The staged name is space-free ASCII because the COMMAND_PTY 'EVENT' parser
    splits on whitespace and does not honour quoting.
    """
    with wave.open(str(src), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        nframes = w.getnframes()
        raw = w.readframes(nframes)

    mono = _frames_to_int16_mono(raw, width, channels)
    mono = _resample_linear(mono, rate, TARGET_RATE)
    if sys.byteorder == "big":
        mono.byteswap()
    duration = len(mono) / float(TARGET_RATE)

    base = (_safe_re.sub("_", src.stem)[:80]).strip("._") or "clip"
    staged = STAGING / f"{base}.{os.getpid()}.{int(time.time())}.wav"
    with wave.open(str(staged), "wb") as o:
        o.setnchannels(1)
        o.setsampwidth(2)
        o.setframerate(TARGET_RATE)
        o.writeframes(mono.tobytes())
    os.chmod(staged, 0o644)
    return staged, duration


# ---------------------------------------------------------------------------
# Channel-idle tracking (tail the SvxLink log)
# ---------------------------------------------------------------------------
class ChannelState:
    _sql_re = re.compile(r"(\S+): The squelch is (OPEN|CLOSED)")
    _tx_re = re.compile(r"Turning the transmitter (ON|OFF)")

    def __init__(self, logpath: str):
        self.logpath = logpath
        self._fh = None
        self._ino = None
        self.open_rx: set[str] = set()
        self.tx_on = False
        self.last_busy = 0.0

    def _ensure_open(self):
        try:
            st = os.stat(self.logpath)
        except FileNotFoundError:
            if self._fh:
                self._fh.close()
                self._fh = None
            return
        if self._fh is None or st.st_ino != self._ino:
            if self._fh:
                self._fh.close()
            self._fh = open(self.logpath, "r", errors="replace")
            self._ino = st.st_ino
            self._fh.seek(0, os.SEEK_END)

    def poll(self):
        self._ensure_open()
        if not self._fh:
            return
        while True:
            line = self._fh.readline()
            if not line:
                break
            m = self._sql_re.search(line)
            if m:
                name, stt = m.group(1), m.group(2)
                if name == "Voter":
                    continue  # aggregate line, not a real receiver
                if stt == "OPEN":
                    self.open_rx.add(name)
                else:
                    self.open_rx.discard(name)
            m = self._tx_re.search(line)
            if m:
                self.tx_on = (m.group(1) == "ON")
        if self.open_rx or self.tx_on:
            self.last_busy = time.monotonic()

    @property
    def busy(self) -> bool:
        return bool(self.open_rx) or self.tx_on


# ---------------------------------------------------------------------------
# Command PTY
# ---------------------------------------------------------------------------
def _svxlink_ready() -> bool:
    return os.path.islink(COMMAND_PTY) or os.path.exists(COMMAND_PTY)


def _pty_write(text: str) -> None:
    fd = os.open(COMMAND_PTY, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
    try:
        os.write(fd, text.encode("ascii", "strict"))
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Queue handling
# ---------------------------------------------------------------------------
def ingest_incoming(seen: dict):
    now = time.monotonic()
    for entry in sorted(INCOMING.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        try:
            st = entry.stat()
        except FileNotFoundError:
            continue
        key = entry.name
        prev = seen.get(key)
        if prev is None or prev[0] != st.st_size or prev[1] != st.st_mtime:
            seen[key] = (st.st_size, st.st_mtime, now)
            continue
        if now - prev[2] < STABLE_S:
            continue
        # stable -> move to queue
        seen.pop(key, None)
        if not entry.name.lower().endswith(".wav") or st.st_size < 44:
            dest = FAILED / entry.name
            _move(entry, dest)
            _write_sidecar(dest, "not a usable .wav file (bad extension or too small)")
            log(f"rejected {entry.name} -> failed/")
            continue
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = _safe_re.sub("_", entry.name)
        dest = QUEUE / f"{stamp}__{safe}"
        _move(entry, dest)
        log(f"queued {entry.name} -> {dest.name}")


def _move(src: Path, dst: Path):
    try:
        src.rename(dst)
    except OSError:
        import shutil
        shutil.move(str(src), str(dst))


def _write_sidecar(dst: Path, text: str):
    try:
        dst.with_suffix(dst.suffix + ".log").write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n"
        )
    except OSError:
        pass


def next_queued() -> Path | None:
    files = sorted(p for p in QUEUE.iterdir() if p.is_file() and not p.name.startswith("."))
    return files[0] if files else None


def wait_for_idle(chan: ChannelState) -> bool:
    """Block until the channel has been idle for IDLE_GUARD_S. Returns False if asked to stop."""
    deadline = time.monotonic() + IDLE_MAX_S
    warned = False
    while not _stop:
        chan.poll()
        svx_ok = _svxlink_ready()
        if svx_ok and not chan.busy:
            idle_for = time.monotonic() - max(chan.last_busy, 0.0)
            if chan.last_busy == 0.0 or idle_for >= IDLE_GUARD_S:
                return True
        if time.monotonic() > deadline:
            log("idle wait timed out - transmitting anyway")
            return True
        if not svx_ok and not warned:
            log(f"waiting: svxlink not ready ({COMMAND_PTY} missing)")
            warned = True
        time.sleep(POLL_S)
    return False


def transmit(path: Path, chan: ChannelState) -> bool:
    log(f"preparing {path.name}")
    if not _svxlink_ready():
        raise RuntimeError(f"{COMMAND_PTY} missing - svxlink not running")

    staged, duration = stage_wav(path)
    try:
        if duration > SVX_TX_TIMEOUT_S:
            raise RuntimeError(
                f"clip is {duration:.0f}s, over the {SVX_TX_TIMEOUT_S:.0f}s limit "
                f"(SimplexLogic TIMEOUT=300); refusing to transmit"
            )
        if not wait_for_idle(chan):
            return False  # stopping; leave original in queue/

        chan.poll()
        keyed = chan.tx_on  # tolerate TX already up (e.g. an ident in progress)
        log(f"transmitting {path.name} ({duration:.1f}s) via {COMMAND_PTY}")
        try:
            _pty_write(f"EVENT ::playFile {staged}\n")
        except OSError as e:
            raise RuntimeError(f"write to {COMMAND_PTY} failed: {e}") from e

        # phase 1: wait for the transmitter to key
        t0 = time.monotonic()
        while not keyed and (time.monotonic() - t0) < TX_ON_TIMEOUT_S:
            if _stop:
                return False
            chan.poll()
            if chan.tx_on:
                keyed = True
                break
            time.sleep(POLL_S)
        if not keyed:
            raise RuntimeError(
                f"transmitter never keyed within {TX_ON_TIMEOUT_S:.0f}s of "
                f"'EVENT ::playFile' (bad file? logic busy? check /var/log/svxlink)"
            )

        # phase 2: wait for a stable transmitter-off, bounded by clip duration
        hard_deadline = time.monotonic() + duration + TX_OFF_MARGIN_S
        while time.monotonic() < hard_deadline:
            if _stop:
                break
            chan.poll()
            if not chan.tx_on:
                time.sleep(1.0)
                chan.poll()
                if not chan.tx_on:
                    break
            time.sleep(POLL_S)
        else:
            log(f"WARNING: {path.name} - transmitter still on at deadline; proceeding")

        log(f"done {path.name}")
        return True
    finally:
        try:
            staged.unlink()
        except OSError:
            pass


def process_queue(chan: ChannelState):
    while not _stop:
        path = next_queued()
        if path is None:
            return
        try:
            ok = transmit(path, chan)
            if not ok:
                return  # stop requested; leave file in queue
            _move(path, SENT / path.name)
        except Exception:
            tb = traceback.format_exc()
            log(f"ERROR transmitting {path.name}:\n{tb}")
            dest = FAILED / path.name
            _move(path, dest)
            _write_sidecar(dest, tb)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--oneshot", action="store_true",
                    help="process whatever is queued/incoming right now, then exit")
    args = ap.parse_args()

    for d in (INCOMING, QUEUE, STAGING, SENT, FAILED):
        d.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    chan = ChannelState(SVXLINK_LOG)
    seen: dict = {}

    log(f"svxlink-txqueue started (spool={SPOOL}, command_pty={COMMAND_PTY}, "
        f"rate={TARGET_RATE})")

    if args.oneshot:
        for _ in range(int(STABLE_S / POLL_S) + 2):
            ingest_incoming(seen)
            time.sleep(POLL_S)
        process_queue(chan)
        log("oneshot complete")
        return

    while not _stop:
        chan.poll()
        ingest_incoming(seen)
        if next_queued() is not None:
            process_queue(chan)
        time.sleep(POLL_S)

    log("stopped")


if __name__ == "__main__":
    main()
```

---

## Appendix B — `/etc/systemd/system/svxlink-txqueue.service`

The canonical copy also lives at
[`svxlink-txqueue/svxlink-txqueue.service`](svxlink-txqueue/svxlink-txqueue.service)
— `svxlink-txqueue-install.sh` deploys that file directly (patching
`User=`/`Group=` if the detected SvxLink user isn't `svxlink`). Kept in sync
with the listing below by hand; if you edit one, edit both.

```ini
[Unit]
Description=SvxLink WAV transmit queue (inject pre-recorded audio on the air)
Documentation=file:/usr/local/bin/svxlink-txqueue
After=svxlink.service
Wants=svxlink.service
BindsTo=svxlink.service

[Service]
Type=simple
User=svxlink
Group=svxlink
ExecStart=/usr/bin/python3 /usr/local/bin/svxlink-txqueue
Restart=on-failure
RestartSec=5

# hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/spool/svxlink-tx /dev/shm
ReadOnlyPaths=/var/log/svxlink
RestrictAddressFamilies=AF_UNIX
IPAddressDeny=any

[Install]
WantedBy=multi-user.target
```

If your SvxLink runs as a user other than `svxlink`, change `User=`/`Group=` and
the spool-dir ownership to match.

---

## Appendix C — porting to a differently-named logic

Everything keys off the logic section name. If your logic is `[MyRepeater]`:

- `svxlink.conf`: put `COMMAND_PTY=/dev/shm/myrepeater_ctrl` in `[MyRepeater]`.
- service: `Environment=TXQUEUE_COMMAND_PTY=/dev/shm/myrepeater_ctrl`.
- The `EVENT ::playFile` call uses the **root** TCL namespace (`::`), so it is
  logic-name-independent — no TCL edits needed.
- The channel-idle log tail matches `Turning the transmitter (ON|OFF)` and
  `<Rx>: The squelch is (OPEN|CLOSED)`, which every logic emits — also
  name-independent.
