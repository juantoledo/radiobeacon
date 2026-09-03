"""Attention tone: a short sequence of beeps prepended to synthesized voice
WAVs so listeners recognize an announcement is starting (like the two-tone
signal on weather/emergency radio).

Shared by beacon.__main__ (prepends the tone to each rendered voice clip
before it goes on air, see _transmit_voice_unit / _transmit_manual_unit) and
ui.routers.config (renders a standalone WAV for the config page's "Preview"
button). It lives here because the beacon and ui processes can't import each
other but both import adapters. Pure stdlib -- no numpy/sox -- same as
beacon.frame_audio.

Spec syntax (BEACON_VOICE_ATTENTION_TONE): comma-separated `freq:ms` pairs,
e.g. "1400:250,0:120,1400:250". freq is Hz (0 = silence); ms is that
segment's duration. An empty string means "no tone".
"""
import io
import logging
import math
import struct
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_SEGMENTS = 16
MAX_TOTAL_MS = 10_000
MAX_FREQ_HZ = 8_000
MIN_SEGMENT_MS = 10
MAX_SEGMENT_MS = 5_000

# Amplitude of a tone segment as a fraction of int16 full scale -- loud
# enough to be unmistakable over a noisy channel, with headroom so it never
# clips.
DEFAULT_AMPLITUDE = 0.5

# Linear fade applied to the start and end of every non-silent segment so the
# waveform starts and ends at zero -- a hard edge is an audible click.
_FADE_MS = 5


class ToneSpecError(ValueError):
    """Raised for a malformed or out-of-bounds BEACON_VOICE_ATTENTION_TONE."""


def parse_tone_spec(spec: str) -> list[tuple[int, int]]:
    """"1400:250,0:120" -> [(1400, 250), (0, 120)]. Raises ToneSpecError on
    bad syntax or values outside the guard rails. An empty/whitespace spec
    returns []."""
    text = (spec or "").strip()
    if not text:
        return []

    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) > MAX_SEGMENTS:
        raise ToneSpecError(f"too many tone segments ({len(parts)}, max {MAX_SEGMENTS})")

    segments: list[tuple[int, int]] = []
    for part in parts:
        if part.count(":") != 1:
            raise ToneSpecError(f"segment {part!r} is not 'freq:ms'")
        freq_s, ms_s = part.split(":")
        try:
            freq, ms = int(freq_s), int(ms_s)
        except ValueError:
            raise ToneSpecError(f"segment {part!r} has a non-integer value") from None
        if not 0 <= freq <= MAX_FREQ_HZ:
            raise ToneSpecError(f"frequency {freq} Hz out of range (0-{MAX_FREQ_HZ})")
        if not MIN_SEGMENT_MS <= ms <= MAX_SEGMENT_MS:
            raise ToneSpecError(
                f"duration {ms} ms out of range ({MIN_SEGMENT_MS}-{MAX_SEGMENT_MS})"
            )
        segments.append((freq, ms))

    total = sum(ms for _, ms in segments)
    if total > MAX_TOTAL_MS:
        raise ToneSpecError(f"total tone length {total} ms exceeds {MAX_TOTAL_MS} ms")
    return segments


def _render_pcm(segments: list[tuple[int, int]], framerate: int, amplitude: float) -> bytes:
    """The tone as raw little-endian int16 mono PCM frames (no WAV header)."""
    peak = max(0.0, min(1.0, amplitude)) * 32767
    out = bytearray()
    for freq, ms in segments:
        n = int(framerate * ms / 1000)
        if n <= 0:
            continue
        if freq <= 0:
            out += b"\x00\x00" * n
            continue
        fade = max(1, int(framerate * _FADE_MS / 1000))
        for i in range(n):
            if i < fade:
                env = i / fade
            elif i > n - fade:
                env = max(0.0, (n - i) / fade)
            else:
                env = 1.0
            out += struct.pack(
                "<h", int(peak * env * math.sin(2 * math.pi * freq * i / framerate))
            )
    return bytes(out)


def render_tone_wav(
    spec: str, *, framerate: int = 22050, amplitude: float = DEFAULT_AMPLITUDE
) -> bytes:
    """A complete standalone WAV (mono, 16-bit) of the tone -- for the config
    page's preview button. Raises ToneSpecError for a bad spec."""
    pcm = _render_pcm(parse_tone_spec(spec), framerate, amplitude)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(pcm)
    return buf.getvalue()


def prepend_tone_to_wav(
    wav_path: Path, spec: str, *, amplitude: float = DEFAULT_AMPLITUDE
) -> None:
    """Rewrite wav_path with the tone prepended. Best-effort, modeled on
    beacon.frame_audio._prepend_silence: logs and leaves the file untouched
    on a bad spec, an unsupported WAV format, or any I/O error -- a missing
    attention tone must never stop a transmission."""
    try:
        segments = parse_tone_spec(spec)
        if not segments:
            return
        with wave.open(str(wav_path), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        if params.sampwidth != 2 or params.nchannels != 1:
            logger.warning(
                "attention_tone: %s is %d-bit %d-channel, expected mono 16-bit "
                "-- skipping tone",
                wav_path, params.sampwidth * 8, params.nchannels,
            )
            return
        tone = _render_pcm(segments, params.framerate, amplitude)
        tmp = wav_path.with_suffix(".tone.wav")
        with wave.open(str(tmp), "wb") as o:
            o.setparams(params)
            o.writeframes(tone + frames)
        tmp.replace(wav_path)
    except (OSError, wave.Error, ToneSpecError):
        logger.warning(
            "attention_tone: could not prepend tone to %s, using as-is",
            wav_path, exc_info=True,
        )
