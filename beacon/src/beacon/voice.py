"""Text-to-speech + voice transmission. TTS itself (text -> WAV) is real
and testable; actually getting that WAV onto the air via SvxLink is not —
CONTEXT.md itself marks SvxLink's remote-control mechanism as an open TODO
("TCL events o comando remoto"), and no real SvxLink instance exists in
this dev environment to validate against. LoggingVoiceTransmitter (the
default) is the only voice path this repo can verify end-to-end right
now."""
import logging
import subprocess
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class VoiceTransmitter(Protocol):
    def transmit(self, *, text: str, wav_path: Path) -> bool: ...


def synthesize_speech(text: str, *, out_path: Path, voice: str = "es") -> bool:
    """text -> WAV via the espeak-ng subprocess (offline, no API key,
    standard on Debian/Ubuntu — apt install espeak-ng). Returns False
    (logs) on a missing binary or non-zero exit; never raises."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["espeak-ng", "-v", voice, "-w", str(out_path), text],
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError:
        logger.error("voice: espeak-ng not found on PATH — cannot synthesize speech")
        return False
    except subprocess.TimeoutExpired:
        logger.error("voice: espeak-ng timed out synthesizing speech")
        return False
    if result.returncode != 0:
        logger.error(
            "voice: espeak-ng exited %d: %s", result.returncode, result.stderr.decode(errors="replace")
        )
        return False
    return True


class LoggingVoiceTransmitter:
    """Default (BEACON_VOICE_TRANSMITTER=logging). Logs what it would play
    instead of touching real hardware/SvxLink — the only voice path
    verifiable in a sandboxed dev environment with no real transceiver."""

    def transmit(self, *, text: str, wav_path: Path) -> bool:
        logger.info("voice [logging transmitter]: would play %s for text=%r", wav_path, text)
        return True


class SvxlinkControlTransmitter:
    """UNVERIFIED STUB (BEACON_VOICE_TRANSMITTER=svxlink). CONTEXT.md
    itself marks SvxLink's remote-control mechanism as an open TODO — the
    most likely real approach is SvxLink's TCL event-handling script
    exposing a custom command (e.g. triggered by dropping/symlinking
    wav_path into SvxLink's configured sound directory and invoking its
    `playFile` TCL command), but that is not implemented here. Raises
    NotImplementedError unconditionally rather than pretending to work —
    do not remove that without validating against a real SvxLink instance
    on the actual shack host first."""

    def transmit(self, *, text: str, wav_path: Path) -> bool:
        raise NotImplementedError(
            "SvxlinkControlTransmitter is an unverified stub — see CONTEXT.md's "
            "own open TODO on SvxLink's remote-control mechanism. Implement and "
            "validate against real hardware before using BEACON_VOICE_TRANSMITTER=svxlink."
        )
