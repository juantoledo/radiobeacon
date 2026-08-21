import sqlite3

import pytest

from dispatcher.policy import (
    RepeatPolicy,
    delete_policy,
    ensure_seeded,
    get_policy,
    list_policies,
    policy_for,
    set_policy,
)


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE dispatch_policies ("
        "name TEXT PRIMARY KEY, repeat_times INTEGER NOT NULL, "
        "interval_seconds INTEGER NOT NULL, description TEXT)"
    )
    return conn


@pytest.fixture
def conn():
    return _make_conn()


def test_ensure_seeded_inserts_defaults_into_empty_table(conn):
    ensure_seeded(conn)

    assert get_policy(conn, "urgent") == RepeatPolicy(repeat_times=5, interval_seconds=60)
    assert get_policy(conn, "informational") == RepeatPolicy(repeat_times=1, interval_seconds=0)


def test_ensure_seeded_is_idempotent_and_does_not_clobber_edits(conn):
    ensure_seeded(conn)
    set_policy(conn, "urgent", repeat_times=9, interval_seconds=15)

    ensure_seeded(conn)  # table is non-empty now — must not re-seed over the edit

    assert get_policy(conn, "urgent") == RepeatPolicy(repeat_times=9, interval_seconds=15)


def test_get_policy_returns_none_for_unknown_name(conn):
    ensure_seeded(conn)

    assert get_policy(conn, "does-not-exist") is None


def test_policy_for_resolves_known_name(conn):
    ensure_seeded(conn)

    assert policy_for(conn, "urgent") == RepeatPolicy(repeat_times=5, interval_seconds=60)


def test_policy_for_falls_back_to_informational_for_none(conn):
    ensure_seeded(conn)

    assert policy_for(conn, None) == get_policy(conn, "informational")


def test_policy_for_falls_back_to_informational_for_unknown_name(conn):
    ensure_seeded(conn)

    assert policy_for(conn, "something-that-does-not-exist") == get_policy(conn, "informational")


def test_policy_for_falls_back_to_hardcoded_default_if_informational_missing(conn):
    ensure_seeded(conn)
    delete_policy(conn, "informational")

    assert policy_for(conn, None) == RepeatPolicy(repeat_times=1, interval_seconds=0)
    assert policy_for(conn, "unknown-name") == RepeatPolicy(repeat_times=1, interval_seconds=0)


def test_set_policy_creates_new_policy(conn):
    set_policy(conn, "custom", repeat_times=2, interval_seconds=5, description="test policy")

    assert get_policy(conn, "custom") == RepeatPolicy(repeat_times=2, interval_seconds=5)


def test_set_policy_upserts_existing_policy(conn):
    set_policy(conn, "custom", repeat_times=2, interval_seconds=5)

    set_policy(conn, "custom", repeat_times=7, interval_seconds=20)

    assert get_policy(conn, "custom") == RepeatPolicy(repeat_times=7, interval_seconds=20)


def test_list_policies_returns_rows_ordered_by_name(conn):
    set_policy(conn, "zeta", repeat_times=1, interval_seconds=0)
    set_policy(conn, "alpha", repeat_times=2, interval_seconds=5)

    rows = list_policies(conn)

    assert [row[0] for row in rows] == ["alpha", "zeta"]


def test_delete_policy_returns_true_when_removed(conn):
    set_policy(conn, "custom", repeat_times=1, interval_seconds=0)

    deleted = delete_policy(conn, "custom")

    assert deleted is True
    assert get_policy(conn, "custom") is None


def test_delete_policy_returns_false_for_unknown_name(conn):
    deleted = delete_policy(conn, "does-not-exist")

    assert deleted is False
