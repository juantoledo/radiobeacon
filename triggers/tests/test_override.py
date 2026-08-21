import sqlite3

from triggers.override import override_item, rearm_item
from triggers.watcher import check_for_new_items, discover_new_items


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE items ("
        "source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "extracted_title TEXT, url TEXT, type TEXT, dispatch_policy TEXT, "
        "PRIMARY KEY (source, item_id))"
    )
    return conn


def _insert_item(conn, source, item_id, dispatch_policy="informational"):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, url, type, dispatch_policy) "
        "VALUES (?, ?, 'Title', 'http://example.test', 'Type', ?)",
        (source, item_id, dispatch_policy),
    )
    conn.commit()


def test_override_item_updates_dispatch_policy():
    conn = _make_conn()
    _insert_item(conn, "csn", "1", dispatch_policy="informational")

    updated = override_item(conn, "csn", "1", dispatch_policy="urgent")

    assert updated is True
    row = conn.execute(
        "SELECT dispatch_policy FROM items WHERE source = 'csn' AND item_id = '1'"
    ).fetchone()
    assert row == ("urgent",)


def test_override_item_can_point_at_any_named_policy():
    conn = _make_conn()
    _insert_item(conn, "csn", "1")

    override_item(conn, "csn", "1", dispatch_policy="custom-policy")

    row = conn.execute(
        "SELECT dispatch_policy FROM items WHERE source = 'csn' AND item_id = '1'"
    ).fetchone()
    assert row == ("custom-policy",)


def test_override_item_returns_false_for_unknown_item():
    conn = _make_conn()

    updated = override_item(conn, "csn", "does-not-exist", dispatch_policy="urgent")

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
    _insert_item(conn, "csn", "1", dispatch_policy="informational")

    # deliver once and let it retire (informational default repeat_times=1)
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


def test_rearm_item_does_nothing_for_item_still_in_flight():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "csn", "1", dispatch_policy="urgent")  # still has redeliveries pending

    delivered = []
    check_for_new_items(conn, consumer, [delivered.append])
    assert len(delivered) == 1

    rearmed = rearm_item(conn, consumer, "csn", "1")

    assert rearmed is False


def test_rearm_item_does_nothing_for_unknown_item():
    conn = _make_conn()

    rearmed = rearm_item(conn, "log", "csn", "does-not-exist")

    assert rearmed is False
