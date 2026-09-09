import io
import wave

import pytest

from adapters.attention_tone import (
    ToneSpecError,
    parse_tone_spec,
    prepend_tone_to_wav,
    render_tone_wav,
)


def _write_wav(path, *, frames=1600, nchannels=1, sampwidth=2, framerate=22050):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(b"\x11\x22" * (frames * nchannels * (sampwidth // 2 or 1)))


# --- parse_tone_spec ---


def test_parse_empty_spec_returns_no_segments():
    assert parse_tone_spec("") == []
    assert parse_tone_spec("   ") == []


def test_parse_multi_segment_spec():
    assert parse_tone_spec("1400:250, 0:120 ,1400:250") == [(1400, 250), (0, 120), (1400, 250)]


@pytest.mark.parametrize(
    "spec",
    [
        "1400",            # missing ms
        "1400:250:1",      # too many parts
        "abc:250",         # non-integer freq
        "1400:xyz",        # non-integer ms
        "9000:250",        # freq over range
        "1400:5",          # ms under range
        "1400:9000",       # ms over range
        ",".join(["440:100"] * 17),   # too many segments
        "440:2000,440:2000,440:2000,440:2000,440:2000,440:2000",  # total over 10 s
    ],
)
def test_parse_rejects_bad_spec(spec):
    with pytest.raises(ToneSpecError):
        parse_tone_spec(spec)


# --- render_tone_wav ---


def test_render_tone_wav_frame_count_matches_durations():
    rate = 22050
    wav = render_tone_wav("1000:200,0:100,1000:200", framerate=rate)
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == rate
        expected = sum(int(rate * ms / 1000) for ms in (200, 100, 200))
        assert w.getnframes() == expected


def test_render_tone_wav_propagates_spec_error():
    with pytest.raises(ToneSpecError):
        render_tone_wav("nonsense")


# --- prepend_tone_to_wav ---


def test_prepend_tone_grows_frame_count(tmp_path):
    p = tmp_path / "voice.wav"
    _write_wav(p, frames=2000, framerate=22050)

    prepend_tone_to_wav(p, "1400:250")

    with wave.open(str(p), "rb") as w:
        assert w.getnframes() == 2000 + int(22050 * 250 / 1000)
        assert w.getframerate() == 22050


def test_prepend_tone_noop_on_empty_spec(tmp_path):
    p = tmp_path / "voice.wav"
    _write_wav(p)
    before = p.read_bytes()

    prepend_tone_to_wav(p, "")

    assert p.read_bytes() == before


def test_prepend_tone_leaves_bad_spec_file_untouched(tmp_path):
    p = tmp_path / "voice.wav"
    _write_wav(p)
    before = p.read_bytes()

    prepend_tone_to_wav(p, "totally invalid")

    assert p.read_bytes() == before


def test_prepend_tone_skips_non_mono_16bit(tmp_path):
    p = tmp_path / "stereo.wav"
    _write_wav(p, nchannels=2)
    before = p.read_bytes()

    prepend_tone_to_wav(p, "1400:250")

    assert p.read_bytes() == before
