import builtins

import pytest

from beacon.ntp import check_offset


def test_check_offset_unavailable_when_ntplib_missing(monkeypatch):
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "ntplib":
            raise ImportError("no module named ntplib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    result = check_offset("pool.ntp.org")

    assert result.ok is False
    assert result.method == "unavailable"
    assert result.offset_seconds is None
    assert result.error is not None


def test_check_offset_ok_on_successful_response(monkeypatch):
    class _FakeResponse:
        offset = 0.042

    class _FakeNtpClient:
        def request(self, server, timeout):
            assert server == "pool.ntp.org"
            return _FakeResponse()

    import ntplib

    monkeypatch.setattr(ntplib, "NTPClient", lambda: _FakeNtpClient())

    result = check_offset("pool.ntp.org")

    assert result.ok is True
    assert result.method == "ntplib"
    assert result.offset_seconds == 0.042
    assert result.error is None


def test_check_offset_not_ok_on_network_failure(monkeypatch):
    class _FakeNtpClient:
        def request(self, server, timeout):
            raise TimeoutError("no route to host")

    import ntplib

    monkeypatch.setattr(ntplib, "NTPClient", lambda: _FakeNtpClient())

    result = check_offset("pool.ntp.org")

    assert result.ok is False
    assert result.offset_seconds is None
    assert result.error is not None


def test_check_offset_never_raises(monkeypatch):
    class _FakeNtpClient:
        def request(self, server, timeout):
            raise RuntimeError("boom")

    import ntplib

    monkeypatch.setattr(ntplib, "NTPClient", lambda: _FakeNtpClient())

    # Should not raise.
    check_offset("pool.ntp.org")
