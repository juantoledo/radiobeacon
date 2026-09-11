import wave

from beacon.transmit import LoggingWavTransmitter, SpoolWavTransmitter


def _make_wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)


def test_logging_wav_transmitter_returns_true(tmp_path):
    assert LoggingWavTransmitter().transmit(wav_path=tmp_path / "x.wav", label="voice a/b") is True


def test_spool_wav_transmitter_places_file_in_incoming_dir(tmp_path):
    src = tmp_path / "src.wav"
    _make_wav(src)
    incoming = tmp_path / "incoming"

    ok = SpoolWavTransmitter(str(incoming)).transmit(wav_path=src, label="frame a/b 0")

    assert ok is True
    dropped = list(incoming.iterdir())
    assert len(dropped) == 1
    assert dropped[0].name.endswith("src.wav")
    assert dropped[0].read_bytes() == src.read_bytes()
    assert not any(p.name.startswith(".") for p in incoming.iterdir())  # no leftover .part


def test_spool_wav_transmitter_creates_incoming_dir(tmp_path):
    src = tmp_path / "src.wav"
    _make_wav(src)
    incoming = tmp_path / "nested" / "incoming"

    assert SpoolWavTransmitter(str(incoming)).transmit(wav_path=src, label="v") is True
    assert incoming.is_dir()


def test_spool_wav_transmitter_returns_false_when_source_missing(tmp_path):
    tx = SpoolWavTransmitter(str(tmp_path / "incoming"))

    ok = tx.transmit(wav_path=tmp_path / "nope.wav", label="v")

    assert ok is False
    # last_error is what __main__.py's _transmit_*_unit functions read into the
    # beacon.*.transmit_failed audit row's "reason" when transmit() fails
    # without raising — see transmit.py's docstring.
    assert "nope.wav" in tx.last_error


def test_spool_wav_transmitter_clears_last_error_after_success(tmp_path):
    src = tmp_path / "src.wav"
    _make_wav(src)
    tx = SpoolWavTransmitter(str(tmp_path / "incoming"))
    tx.last_error = "stale from a previous failed attempt"

    ok = tx.transmit(wav_path=src, label="v")

    assert ok is True
    assert tx.last_error is None


def test_spool_wav_transmitter_captures_permission_error_message(tmp_path, monkeypatch):
    src = tmp_path / "src.wav"
    _make_wav(src)
    tx = SpoolWavTransmitter(str(tmp_path / "incoming"))
    monkeypatch.setattr(
        "beacon.transmit.os.rename",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied")),
    )

    ok = tx.transmit(wav_path=src, label="v")

    assert ok is False
    assert "Permission denied" in tx.last_error
