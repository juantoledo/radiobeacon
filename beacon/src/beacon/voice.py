"""Text-to-speech: text -> WAV. Getting that WAV on the air is handled
generically for both beacon types by transmit.py (drop the WAV into the
svxlink-txqueue spool).

Two TTS engines are selectable (BEACON_TTS_ENGINE): "piper" (default —
neural, noticeably more natural; ships with a default es_MX (Latin
American Spanish) voice model auto-downloaded by start.sh into
beacon/storage/piper_voices/, see BEACON_TTS_PIPER_MODEL) and "espeak"
(robotic but zero-setup, offline fallback — apt install espeak-ng)."""
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def synthesize_speech(
    text: str,
    *,
    out_path: Path,
    voice: str = "es",
    engine: str = "espeak",
    piper_model: str = "",
    piper_binary: str = "piper",
) -> bool:
    """text -> WAV. Dispatches to the configured engine; never raises."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if engine == "piper":
        return _synthesize_speech_piper(text, out_path=out_path, model_path=piper_model, binary=piper_binary)
    return _synthesize_speech_espeak(text, out_path=out_path, voice=voice)


def _synthesize_speech_espeak(text: str, *, out_path: Path, voice: str) -> bool:
    """text -> WAV via the espeak-ng subprocess (offline, no API key,
    standard on Debian/Ubuntu — apt install espeak-ng). Returns False
    (logs) on a missing binary or non-zero exit; never raises."""
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


def _synthesize_speech_piper(text: str, *, out_path: Path, model_path: str, binary: str) -> bool:
    """text -> WAV via the piper subprocess (offline neural TTS, no API
    key — pip install piper-tts, or the standalone binary release). Needs
    a downloaded .onnx voice model (BEACON_TTS_PIPER_MODEL) plus its
    .onnx.json sidecar in the same directory; text is piped over stdin,
    matching piper's own CLI contract. Returns False (logs) on a missing
    binary, unconfigured model, or non-zero exit; never raises."""
    if not model_path:
        logger.error("voice: BEACON_TTS_PIPER_MODEL not configured — cannot synthesize speech via piper")
        return False
    try:
        result = subprocess.run(
            [binary, "--model", model_path, "--output_file", str(out_path)],
            input=text.encode(),
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError:
        logger.error("voice: piper not found on PATH — cannot synthesize speech")
        return False
    except subprocess.TimeoutExpired:
        logger.error("voice: piper timed out synthesizing speech")
        return False
    if result.returncode != 0:
        logger.error(
            "voice: piper exited %d: %s", result.returncode, result.stderr.decode(errors="replace")
        )
        return False
    return True
