"""AX.25 UI frame -> WAV, by shelling out to Direwolf's `gen_packets` CLI.

The beacon no longer talks to a running Direwolf over KISS/TCP — the whole
transmit path is now "render a WAV, drop it in the svxlink-txqueue spool, let
SvxLink play it when the channel is idle" (see
documentation/svxlink-txqueue-SETUP.md). For the `frame` beacon type that means
turning the assembled TNC2 line into an AFSK-modulated WAV up front.

`gen_packets` (shipped with the `direwolf` package — `apt install direwolf`) does
exactly that: given a file of TNC2 monitor-format lines it emits a Bell 202
1200-baud AFSK .wav with proper HDLC framing, flags and FCS. It is invoked as a
one-shot renderer here; no Direwolf process ever runs.

synthesize_frame_wav mirrors voice.synthesize_speech's contract: it never
raises, returns a bool, and logs on failure.
"""
import logging
import subprocess
import tempfile
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

TARGET_RATE = 16000  # SvxLink's internal rate; matches svxlink-txqueue's TXQUEUE_TARGET_RATE


def synthesize_frame_wav(
    tnc2_line: str,
    *,
    out_path: Path,
    gen_packets_binary: str = "gen_packets",
    lead_silence_ms: int = 250,
) -> bool:
    """TNC2 line (e.g. "N0CALL-1>NFO:content") -> 16 kHz mono AFSK WAV at
    out_path. Returns False (logs) on a missing binary, non-zero exit, timeout,
    or unreadable output; never raises.

    If lead_silence_ms > 0, that much silence is prepended so the first bits
    aren't clipped while SvxLink keys the transmitter.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    line = tnc2_line.replace("\n", " ").replace("\r", " ").strip()
    if not line:
        logger.error("empty TNC2 line, nothing to render")
        return False

    # No trailing newline: gen_packets includes it in the info field as a
    # stray 0x0a byte on the air otherwise.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", dir=str(out_path.parent), delete=False, encoding="utf-8"
    ) as tf:
        tf.write(line)
        tf_path = Path(tf.name)

    try:
        result = subprocess.run(
            [gen_packets_binary, "-o", str(out_path), "-r", str(TARGET_RATE), str(tf_path)],
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError:
        logger.error("%s not found on PATH — cannot render frame WAV", gen_packets_binary)
        return False
    except subprocess.TimeoutExpired:
        logger.error("%s timed out rendering frame WAV", gen_packets_binary)
        return False
    finally:
        try:
            tf_path.unlink()
        except OSError:
            pass

    if result.returncode != 0:
        logger.error(
            "%s exited %d: %s",
            gen_packets_binary, result.returncode, result.stderr.decode(errors="replace"),
        )
        return False
    if not out_path.exists() or out_path.stat().st_size < 44:
        logger.error("%s produced no usable WAV at %s", gen_packets_binary, out_path)
        return False

    if lead_silence_ms > 0:
        _prepend_silence(out_path, lead_silence_ms)
    return True


def _prepend_silence(wav_path: Path, ms: int) -> None:
    """Rewrite wav_path with `ms` milliseconds of leading silence. Best-effort —
    logs and leaves the original untouched on any error."""
    try:
        with wave.open(str(wav_path), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        silent = b"\x00" * (int(params.framerate * ms / 1000) * params.sampwidth * params.nchannels)
        tmp = wav_path.with_suffix(".pad.wav")
        with wave.open(str(tmp), "wb") as o:
            o.setparams(params)
            o.writeframes(silent + frames)
        tmp.replace(wav_path)
    except (OSError, wave.Error):
        logger.warning("could not prepend silence to %s, using as-is", wav_path, exc_info=True)
