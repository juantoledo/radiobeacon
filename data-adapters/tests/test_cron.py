from datetime import datetime, timezone

import pytest

from adapters import cron
from adapters.storage import get_connection, set_setting


@pytest.fixture
def conn(tmp_path):
    c = get_connection(tmp_path / "radiobeacon.db")
    # America/Santiago is UTC-04:00 on 2026-09-03 (no DST edge that day).
    set_setting(c, "DISPLAY_TIMEZONE", "America/Santiago")
    return c


def test_is_valid_cron():
    assert cron.is_valid_cron("0 6 * * *")
    assert cron.is_valid_cron("*/5 * * * *")
    assert not cron.is_valid_cron("")
    assert not cron.is_valid_cron("   ")
    assert not cron.is_valid_cron("not a cron")
    assert not cron.is_valid_cron("99 99 * * *")


def test_latest_fire_at_or_before_is_display_timezone_aware(conn):
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)  # 08:00 in Santiago

    latest = cron.latest_fire_at_or_before("0 6 * * *", now, conn=conn)

    # 06:00 Santiago on the 3rd == 10:00 UTC
    assert latest == datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)


def test_latest_fire_before_todays_occurrence_is_yesterday(conn):
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)  # 05:00 Santiago, before 06:00

    latest = cron.latest_fire_at_or_before("0 6 * * *", now, conn=conn)

    assert latest == datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)


def test_latest_fire_is_inclusive_of_an_exact_occurrence(conn):
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)  # exactly 06:00 Santiago

    latest = cron.latest_fire_at_or_before("0 6 * * *", now, conn=conn)

    assert latest == now


def test_next_fire_after(conn):
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    nxt = cron.next_fire_after("0 6 * * *", now, conn=conn)

    assert nxt == datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)


def test_invalid_expression_returns_none(conn):
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    assert cron.latest_fire_at_or_before("nope", now, conn=conn) is None
    assert cron.next_fire_after("nope", now, conn=conn) is None
