from datetime import datetime, timedelta, timezone

import pytest

from adapters.timeutil import to_display_tz, to_utc, utc_now


def test_utc_now_is_aware_and_utc():
    now = utc_now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_to_utc_converts_aware_datetime_regardless_of_offset():
    chile = datetime(2026, 8, 19, 13, 28, 28, tzinfo=timezone(timedelta(hours=-4)))
    utc = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

    assert to_utc(chile) == datetime(2026, 8, 19, 17, 28, 28, tzinfo=timezone.utc)
    assert to_utc(utc) == utc


def test_to_utc_raises_without_assume_tz_for_naive_datetime():
    naive = datetime(2026, 8, 21, 12, 14, 57)

    with pytest.raises(ValueError):
        to_utc(naive)


def test_to_utc_converts_naive_datetime_with_assume_tz():
    naive = datetime(2026, 8, 21, 12, 14, 57)  # winter, Chile is -04:00

    result = to_utc(naive, assume_tz="America/Santiago")

    assert result == datetime(2026, 8, 21, 16, 14, 57, tzinfo=timezone.utc)


def test_to_utc_converts_naive_datetime_with_assume_tz_during_dst():
    naive = datetime(2026, 1, 15, 12, 14, 57)  # summer, Chile is -03:00 (DST)

    result = to_utc(naive, assume_tz="America/Santiago")

    assert result == datetime(2026, 1, 15, 15, 14, 57, tzinfo=timezone.utc)


def test_to_display_tz_raises_for_naive_datetime():
    naive = datetime(2026, 8, 21, 18, 0, 0)

    with pytest.raises(ValueError):
        to_display_tz(naive)


def test_to_display_tz_defaults_to_america_santiago(monkeypatch):
    monkeypatch.delenv("DISPLAY_TIMEZONE", raising=False)
    utc = datetime(2026, 8, 21, 18, 0, 0, tzinfo=timezone.utc)  # winter, -04:00

    result = to_display_tz(utc)

    assert result == datetime(2026, 8, 21, 14, 0, 0, tzinfo=timezone(timedelta(hours=-4)))


def test_to_display_tz_overridable_via_env_var(monkeypatch):
    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
    utc = datetime(2026, 8, 21, 18, 0, 0, tzinfo=timezone.utc)

    result = to_display_tz(utc)

    assert result.hour == 18
    assert result.utcoffset() == timedelta(0)


def test_to_display_tz_accepts_any_tz_aware_input_not_just_utc():
    chile = datetime(2026, 8, 21, 14, 0, 0, tzinfo=timezone(timedelta(hours=-4)))

    result = to_display_tz(chile)

    assert result == chile
