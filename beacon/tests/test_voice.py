import shutil

import pytest

from beacon.voice import LoggingVoiceTransmitter, SvxlinkControlTransmitter, synthesize_speech


def test_logging_voice_transmitter_returns_true(tmp_path):
    transmitter = LoggingVoiceTransmitter()

    assert transmitter.transmit(text="hola", wav_path=tmp_path / "out.wav") is True


def test_svxlink_control_transmitter_is_an_unverified_stub(tmp_path):
    transmitter = SvxlinkControlTransmitter()

    with pytest.raises(NotImplementedError):
        transmitter.transmit(text="hola", wav_path=tmp_path / "out.wav")


def test_synthesize_speech_returns_false_when_binary_missing(tmp_path, monkeypatch):
    import subprocess

    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", _raise_not_found)

    assert synthesize_speech("hola", out_path=tmp_path / "out.wav") is False


def test_synthesize_speech_returns_false_on_nonzero_exit(tmp_path, monkeypatch):
    import subprocess

    class _FakeResult:
        returncode = 1
        stderr = b"boom"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult())

    assert synthesize_speech("hola", out_path=tmp_path / "out.wav") is False


def test_synthesize_speech_creates_parent_directory(tmp_path, monkeypatch):
    import subprocess

    class _FakeResult:
        returncode = 0
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult())
    out_path = tmp_path / "nested" / "dir" / "out.wav"

    synthesize_speech("hola", out_path=out_path)

    assert out_path.parent.is_dir()


@pytest.mark.skipif(shutil.which("espeak-ng") is None, reason="espeak-ng not installed")
def test_synthesize_speech_produces_a_real_wav_file(tmp_path):
    """Only checks that a non-empty file gets produced -- audio
    correctness/intelligibility isn't verifiable by any automated test."""
    out_path = tmp_path / "out.wav"

    ok = synthesize_speech("hola mundo", out_path=out_path)

    assert ok is True
    assert out_path.exists()
    assert out_path.stat().st_size > 0
