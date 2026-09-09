"""The single on-air hand-off: a rendered WAV -> the svxlink-txqueue spool.

Both beacon types (voice, frame) now produce a WAV file. Putting that WAV on the
air is one operation: drop it into svxlink-txqueue's `incoming/` folder and let
that service play it through SvxLink when the RF channel is idle (see
documentation/svxlink-txqueue-SETUP.md). This replaces both the old
voice-transmitter abstraction and the KISS/TCP-to-Direwolf frame path.

LoggingWavTransmitter (the default) is the only path verifiable in a dev
environment with no spool folder / no SvxLink.
"""
import logging
import os
import time
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class WavTransmitter(Protocol):
    def transmit(self, *, wav_path: Path, label: str) -> bool: ...


class LoggingWavTransmitter:
    """Default (BEACON_WAV_TRANSMITTER=logging). Logs what it would hand off
    instead of touching a real spool folder."""

    def transmit(self, *, wav_path: Path, label: str) -> bool:
        logger.info("would drop into spool transmitter=logging clip=%s label=%s", wav_path, label)
        return True


class SpoolWavTransmitter:
    """BEACON_WAV_TRANSMITTER=spool. Copies the WAV into svxlink-txqueue's
    incoming folder via a write-then-rename so the watcher never sees a partial
    file. Returns False (logs) on any OSError; never raises."""

    def __init__(self, incoming_dir: str):
        self._incoming_dir = Path(incoming_dir)

    def transmit(self, *, wav_path: Path, label: str) -> bool:
        try:
            self._incoming_dir.mkdir(parents=True, exist_ok=True)
            data = wav_path.read_bytes()
            final = self._incoming_dir / f"{int(time.time() * 1000)}-{wav_path.name}"
            tmp = final.with_name(f".{final.name}.part")
            tmp.write_bytes(data)
            os.rename(tmp, final)
        except OSError:
            logger.error("failed to place clip in spool transmitter=spool clip=%s dir=%s label=%s",
                         wav_path, self._incoming_dir, label, exc_info=True)
            return False
        logger.info("queued clip transmitter=spool clip=%s label=%s", final, label)
        return True
