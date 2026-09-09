"""Live transmit-state monitor for the dashboard's "ON AIR" indicator.

radiobeacon hands audio to SvxLink one way only: a rendered WAV dropped
into the svxlink-txqueue spool (see transmit.SpoolWavTransmitter). The
real keying happens later, asynchronously, inside the out-of-repo
svxlink-txqueue service once the RF channel is idle. The beacon process
therefore never learns when the transmitter is actually on the air.

This thread closes that gap for a purely cosmetic dashboard indicator: it
tails the SvxLink log for ``Turning the transmitter (ON|OFF)`` -- the
exact line svxlink-txqueue itself keys off (see
documentation/svxlink-txqueue-SETUP.md) -- and records the state in the
``beacon_status`` table for ui/ to render.

If the log file is absent or unreadable (SvxLink on another host, in a
container, or simply not configured yet), the monitor stays dormant and
writes nothing: ``tx_monitor_heartbeat_at`` goes stale and the UI shows
nothing at all. Transmission is never touched -- this is read-only.
"""
import logging
import os
import re
import threading
import time

from adapters.beacon_defaults import (
    BEACON_SVXLINK_LOG_PATH_DEFAULT,
    BEACON_TX_MONITOR_ENABLED_DEFAULT,
)
from adapters.storage import (
    DEFAULT_DB_PATH,
    get_connection,
    get_setting,
    set_beacon_status,
)
from adapters.timeutil import utc_now

logger = logging.getLogger(__name__)

# Every SvxLink logic emits this on key-up / key-down, regardless of logic
# type -- same match svxlink-txqueue uses for channel-idle tracking.
_TX_RE = re.compile(r"Turning the transmitter (ON|OFF)")

# readline() at EOF is nearly free; a short poll keeps the dashboard's
# key-up / key-down latency low.
_POLL_SECONDS = 0.5

# Don't stat() a missing/unreadable log on every poll.
_RETRY_SECONDS = 30.0

# Proof-of-life written at most this often (the UI treats the monitor as
# dead once this key is older than its own, larger, staleness window).
_HEARTBEAT_SECONDS = 2.0

# Saw ON but never an OFF for this long -> assume the OFF line was missed
# (svxlink crash, log-rotation gap) and force the state back to idle so
# the indicator can't stay pinned on.
_MAX_KEYED_SECONDS = 300.0


class _LogTail:
    """A follow-the-file reader that survives log rotation."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None
        self._ino = None

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
                self._ino = None

    def ensure_open(self) -> bool:
        """True when a handle positioned for tailing is ready."""
        if not self.path:
            self.close()
            return False
        try:
            st = os.stat(self.path)
        except OSError:
            self.close()
            return False
        if self._fh is not None and st.st_ino == self._ino:
            return True
        rotated = self._fh is not None
        self.close()
        try:
            fh = open(self.path, "r", errors="replace")
        except OSError:
            return False
        # First open tails from the end so history isn't replayed; a
        # rotation opens the fresh file at the start so the first lines
        # after the swap aren't lost.
        if not rotated:
            fh.seek(0, os.SEEK_END)
        self._fh = fh
        self._ino = st.st_ino
        return True

    def readlines(self):
        if self._fh is None:
            return
        while True:
            line = self._fh.readline()
            if not line:
                break
            yield line


def _record_keyed(conn, keyed: bool) -> None:
    now = utc_now().isoformat()
    if keyed:
        set_beacon_status(conn, "tx_keyed", "1")
        set_beacon_status(conn, "tx_keyed_at", now)
    else:
        set_beacon_status(conn, "tx_keyed", "0")
        set_beacon_status(conn, "tx_unkeyed_at", now)


def run_tx_monitor(stop_event: threading.Event) -> None:
    """Thread target -- see module docstring. Never raises out."""
    conn = get_connection(DEFAULT_DB_PATH)
    tail = _LogTail("")
    active = False          # currently tailing a readable log
    keyed = False           # last transmitter state we recorded
    keyed_since = 0.0       # monotonic, for the stuck-ON watchdog
    last_heartbeat = 0.0    # monotonic

    logger.info("tx monitor thread starting")
    try:
        while not stop_event.is_set():
            enabled = (
                get_setting(
                    "BEACON_TX_MONITOR_ENABLED",
                    BEACON_TX_MONITOR_ENABLED_DEFAULT,
                    conn=conn,
                ).strip().lower()
                == "true"
            )
            path = get_setting(
                "BEACON_SVXLINK_LOG_PATH", BEACON_SVXLINK_LOG_PATH_DEFAULT, conn=conn
            ).strip()

            if path != tail.path:
                tail.close()
                tail.path = path
                active = False

            if not (enabled and tail.ensure_open()):
                if active or keyed:
                    logger.info(
                        "tx monitor dormant (svxlink log unavailable or monitor disabled)"
                    )
                    if keyed:
                        _record_keyed(conn, False)
                    active = False
                    keyed = False
                stop_event.wait(_RETRY_SECONDS)
                continue

            if not active:
                logger.info("tx monitor tailing %s for transmit state", path)
                active = True

            for line in tail.readlines():
                m = _TX_RE.search(line)
                if m is None:
                    continue
                want = m.group(1) == "ON"
                if want != keyed:
                    keyed = want
                    keyed_since = time.monotonic()
                    _record_keyed(conn, keyed)

            if keyed and time.monotonic() - keyed_since > _MAX_KEYED_SECONDS:
                logger.warning(
                    "transmitter keyed for >%.0fs with no OFF line -- forcing idle",
                    _MAX_KEYED_SECONDS,
                )
                keyed = False
                _record_keyed(conn, False)

            if time.monotonic() - last_heartbeat >= _HEARTBEAT_SECONDS:
                set_beacon_status(conn, "tx_monitor_heartbeat_at", utc_now().isoformat())
                last_heartbeat = time.monotonic()

            stop_event.wait(_POLL_SECONDS)
    finally:
        tail.close()
        conn.close()
        logger.info("tx monitor thread stopped")
