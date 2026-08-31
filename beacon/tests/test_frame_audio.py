import subprocess
import wave

from beacon import frame_audio


class _FakeResult:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


def _write_wav(path, frames=1600):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x11\x22" * frames)


def test_synthesize_frame_wav_invokes_gen_packets_with_rate_and_tnc2_file(tmp_path, monkeypatch):
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["tnc2"] = open(cmd[-1]).read()  # trailing TNC2 input file
        _write_wav(tmp_path / "out.wav")
        return _FakeResult()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    ok = frame_audio.synthesize_frame_wav(
        "CD3DXZ-1>NFO:hello world", out_path=tmp_path / "out.wav", lead_silence_ms=0
    )

    assert ok is True
    assert captured["cmd"][0] == "gen_packets"
    assert "-o" in captured["cmd"] and str(tmp_path / "out.wav") in captured["cmd"]
    assert "-r" in captured["cmd"] and "16000" in captured["cmd"]
    assert captured["tnc2"].strip() == "CD3DXZ-1>NFO:hello world"


def test_synthesize_frame_wav_prepends_lead_silence(tmp_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        _write_wav(tmp_path / "out.wav", frames=1600)
        return _FakeResult()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    ok = frame_audio.synthesize_frame_wav(
        "CD3DXZ-1>NFO:x", out_path=tmp_path / "out.wav", lead_silence_ms=250
    )

    assert ok is True
    with wave.open(str(tmp_path / "out.wav"), "rb") as w:
        # 1600 original + 0.25s * 16000 = 4000 leading silent frames
        assert w.getnframes() == 1600 + 4000


def test_synthesize_frame_wav_returns_false_when_binary_missing(tmp_path, monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", _raise)
    assert frame_audio.synthesize_frame_wav("A>B:c", out_path=tmp_path / "o.wav") is False


def test_synthesize_frame_wav_returns_false_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult(returncode=1, stderr=b"boom"))
    assert frame_audio.synthesize_frame_wav("A>B:c", out_path=tmp_path / "o.wav") is False


def test_synthesize_frame_wav_returns_false_when_no_output_produced(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult())  # exits 0, writes nothing
    assert frame_audio.synthesize_frame_wav("A>B:c", out_path=tmp_path / "o.wav") is False


def test_synthesize_frame_wav_returns_false_on_empty_line(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult())
    assert frame_audio.synthesize_frame_wav("   ", out_path=tmp_path / "o.wav") is False
