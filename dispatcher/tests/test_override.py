import sqlite3

from dispatcher.override import override_item, rearm_item, reset_dispatch_state
from dispatcher.watcher import check_for_new_items, discover_new_items


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE items ("
        "source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "extracted_title TEXT, url TEXT, type TEXT, policy TEXT, "
        "source_date_time TEXT, "
        "PRIMARY KEY (source, item_id))"
    )
    return conn


def _insert_item(conn, source, item_id, policy="informational"):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, url, type, policy) "
        "VALUES (?, ?, 'Title', 'http://example.test', 'Type', ?)",
        (source, item_id, policy),
    )
    conn.commit()


def test_override_item_updates_policy():
    conn = _make_conn()
    _insert_item(conn, "csn", "1", policy="informational")

    updated = override_item(conn, "csn", "1", policy="urgent")

    assert updated is True
    row = conn.execute(
        "SELECT policy FROM items WHERE source = 'csn' AND item_id = '1'"
    ).fetchone()
    assert row == ("urgent",)


def test_override_item_can_point_at_any_named_policy():
    conn = _make_conn()
    _insert_item(conn, "csn", "1")

    override_item(conn, "csn", "1", policy="custom-policy")

    row = conn.execute(
        "SELECT policy FROM items WHERE source = 'csn' AND item_id = '1'"
    ).fetchone()
    assert row == ("custom-policy",)


def test_override_item_returns_false_for_unknown_item():
    conn = _make_conn()

    updated = override_item(conn, "csn", "does-not-exist", policy="urgent")

    assert updated is False


def test_override_item_returns_false_when_nothing_passed():
    conn = _make_conn()
    _insert_item(conn, "csn", "1")

    updated = override_item(conn, "csn", "1")

    assert updated is False


def test_rearm_item_reinserts_dispatch_row_for_retired_item():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "csn", "1", policy="informational")

    # deliver once and let it retire (dispatcher delivers exactly once)
    delivered = []
    check_for_new_items(conn, consumer, [delivered.append])
    assert len(delivered) == 1
    remaining = conn.execute(
        "SELECT COUNT(*) FROM trigger_dispatches WHERE consumer = ?", (consumer,)
    ).fetchone()[0]
    assert remaining == 0

    rearmed = rearm_item(conn, consumer, "csn", "1")
    assert rearmed is True

    check_for_new_items(conn, consumer, [delivered.append])
    assert len(delivered) == 2


def test_rearm_item_does_nothing_for_item_still_armed():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)  # establish watermark
    _insert_item(conn, "csn", "1", policy="urgent")
    discover_new_items(conn, consumer)  # arms it, but nothing dispatched yet

    rearmed = rearm_item(conn, consumer, "csn", "1")

    assert rearmed is False


def test_rearm_item_does_nothing_for_unknown_item():
    conn = _make_conn()

    rearmed = rearm_item(conn, "log", "csn", "does-not-exist")

    assert rearmed is False


def test_override_item_records_audit_event():
    conn = _make_conn()
    _insert_item(conn, "csn", "1", policy="informational")

    override_item(conn, "csn", "1", policy="urgent")

    row = conn.execute(
        "SELECT event_type, source, item_id, details "
        "FROM audit_log WHERE event_type = 'item.policy_overridden'"
    ).fetchone()
    assert row[0] == "item.policy_overridden"
    assert row[1] == "csn"
    assert row[2] == "1"
    assert '"policy": "urgent"' in row[3]


def test_rearm_item_records_audit_event():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "csn", "1", policy="informational")
    check_for_new_items(conn, consumer, [lambda row: None])  # deliver + retire

    rearm_item(conn, consumer, "csn", "1")

    row = conn.execute(
        "SELECT event_type, source, item_id FROM audit_log WHERE event_type = 'item.reprocessed'"
    ).fetchone()
    assert tuple(row) == ("item.reprocessed", "csn", "1")


def test_reset_dispatch_state_clears_in_flight_row():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)  # establishes the watermark at 0 first
    _insert_item(conn, "csn", "1", policy="urgent")
    discover_new_items(conn, consumer)  # now discovers + arms it

    in_flight_before = conn.execute(
        "SELECT COUNT(*) FROM trigger_dispatches WHERE consumer = ?", (consumer,)
    ).fetchone()[0]
    assert in_flight_before == 1

    cleared = reset_dispatch_state(conn, consumer, "csn", "1")

    assert cleared is True
    in_flight_after = conn.execute(
        "SELECT COUNT(*) FROM trigger_dispatches WHERE consumer = ?", (consumer,)
    ).fetchone()[0]
    assert in_flight_after == 0


def test_reset_dispatch_state_clears_item_policy_state():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "csn", "1", policy="informational")
    discover_new_items(conn, consumer)  # baselines item_policy_state for this item

    reset_dispatch_state(conn, consumer, "csn", "1")

    row = conn.execute(
        "SELECT 1 FROM item_policy_state WHERE consumer = ? AND source = 'csn' AND item_id = '1'",
        (consumer,),
    ).fetchone()
    assert row is None


def test_reset_dispatch_state_does_not_rearm():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "csn", "1", policy="informational")
    discover_new_items(conn, consumer)

    reset_dispatch_state(conn, consumer, "csn", "1")

    # Neither in-flight nor scheduled again — a plain poll must not
    # redeliver it (distinguishing this from rearm_item, which would).
    delivered = []
    check_for_new_items(conn, consumer, [delivered.append])
    assert delivered == []


def test_reset_dispatch_state_returns_false_when_nothing_to_clear():
    conn = _make_conn()

    cleared = reset_dispatch_state(conn, "log", "csn", "does-not-exist")

    assert cleared is False


def test_reset_dispatch_state_records_audit_event():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "csn", "1", policy="informational")
    discover_new_items(conn, consumer)

    reset_dispatch_state(conn, consumer, "csn", "1")

    row = conn.execute(
        "SELECT event_type, source, item_id FROM audit_log "
        "WHERE event_type = 'item.dispatch_state_reset'"
    ).fetchone()
    assert tuple(row) == ("item.dispatch_state_reset", "csn", "1")
