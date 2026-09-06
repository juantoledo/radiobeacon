import pytest

from adapters.policy import (
    DEFAULT_POLICY_NAME,
    Schedule,
    delete_policy,
    describe_policy,
    get_policy,
    list_policies,
    policy_for,
    policy_reference_count,
    resolve_policy,
    set_policy,
)
from adapters.storage import get_connection, set_adapter_instance


@pytest.fixture
def conn(tmp_path):
    return get_connection(tmp_path / "radiobeacon.db")


def test_default_policy_is_seeded(conn):
    p = resolve_policy(conn, "default")
    assert p.name == "default"
    assert p.fetch == Schedule(kind="interval", interval_seconds=10)
    assert p.transmit == Schedule(kind="once", count=1)


def test_resolve_policy_falls_back_to_default_for_unknown_and_none(conn):
    assert resolve_policy(conn, None).name == "default"
    assert resolve_policy(conn, "nope").name == "default"


def test_resolve_policy_hardcoded_fallback_when_default_missing(conn):
    delete_policy(conn, "default")
    p = resolve_policy(conn, None)
    assert p.fetch.kind == "interval"
    assert p.transmit.kind == "once"
    assert p.transmit.count == 1


def test_set_policy_interval_transmit(conn):
    set_policy(
        conn,
        "urgent",
        transmit_kind="interval",
        transmit_count=5,
        transmit_interval_seconds=60,
        description="loud",
    )
    sched = policy_for(conn, "urgent")
    assert sched == Schedule(kind="interval", interval_seconds=60, count=5)


def test_set_policy_cron_fetch_and_transmit(conn):
    set_policy(
        conn,
        "forecast",
        fetch_kind="cron",
        fetch_cron="0 21 * * *",
        transmit_kind="cron",
        transmit_cron="0 7,19 * * *",
        transmit_count=2,
    )
    p = resolve_policy(conn, "forecast")
    assert p.fetch == Schedule(kind="cron", cron="0 21 * * *")
    assert p.transmit == Schedule(kind="cron", cron="0 7,19 * * *", count=2)


def test_set_policy_once_forces_count_one(conn):
    set_policy(conn, "o", transmit_kind="once", transmit_count=9)
    assert get_policy(conn, "o").transmit_count == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fetch_kind": "cron", "fetch_cron": "not a cron"},
        {"fetch_kind": "bogus"},
        {"transmit_kind": "interval", "transmit_count": 0, "transmit_interval_seconds": 5},
        {"transmit_kind": "interval", "transmit_count": 3, "transmit_interval_seconds": 0},
        {"transmit_kind": "cron", "transmit_cron": ""},
    ],
)
def test_set_policy_rejects_incoherent(conn, kwargs):
    with pytest.raises(ValueError):
        set_policy(conn, "bad", **kwargs)


def test_list_policies_ordered_by_name(conn):
    set_policy(conn, "zeta")
    set_policy(conn, "alpha")
    assert [p.name for p in list_policies(conn)] == ["alpha", "default", "zeta"]


def test_delete_policy(conn):
    set_policy(conn, "tmp")
    assert delete_policy(conn, "tmp") is True
    assert get_policy(conn, "tmp") is None
    assert delete_policy(conn, "tmp") is False


def test_policy_reference_count(conn):
    set_policy(conn, "shared")
    set_adapter_instance(conn, "a", "api", {"url": "https://x.test"}, policy="shared")
    conn.execute("INSERT INTO items (source,item_id,policy,fetched_at,rawdata) VALUES ('a','1','shared','t','{}')")
    conn.commit()
    counts = policy_reference_count(conn, "shared")
    assert counts == {"adapters": 1, "items": 1}


def test_set_policy_records_audit_event(conn):
    set_policy(conn, "custom", description="x")
    row = conn.execute(
        "SELECT actor, details FROM audit_log WHERE event_type = 'policy.set' "
        "AND details LIKE '%custom%'"
    ).fetchone()
    assert row[0] == "adapters.policy"


def test_describe_policy_reads_naturally(conn):
    set_policy(conn, "d", transmit_kind="interval", transmit_count=3, transmit_interval_seconds=60)
    assert describe_policy(resolve_policy(conn, "d")) == "fetches every 10s · airs 3x every 60s"


def test_default_policy_name_constant():
    assert DEFAULT_POLICY_NAME == "default"
