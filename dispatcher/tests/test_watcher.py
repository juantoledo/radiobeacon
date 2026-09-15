import sqlite3
from datetime import datetime, timezone

import dispatcher.watcher as watcher_module
from dispatcher.watcher import _ensure_tables, check_for_new_items, discover_new_items

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


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


def _insert_item(
    conn,
    source,
    item_id,
    policy="informational",
    title="Title",
    url="http://example.test",
    source_date_time=None,
):
    conn.execute(
        "INSERT INTO items "
        "(source, item_id, extracted_title, url, type, policy, source_date_time) "
        "VALUES (?, ?, ?, ?, 'Type', ?, ?)",
        (source, item_id, title, url, policy, source_date_time),
    )
    conn.commit()


def _armed(conn, consumer, source, item_id):
    return conn.execute(
        "SELECT 1 FROM trigger_dispatches "
        "WHERE consumer = ? AND source = ? AND item_id = ?",
        (consumer, source, item_id),
    ).fetchone() is not None


def test_ensure_tables_creates_tables_idempotently():
    conn = _make_conn()

    _ensure_tables(conn)
    _ensure_tables(conn)  # idempotent, no error

    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"dispatcher_state", "trigger_dispatches", "item_policy_state"} <= tables
    # The policy table is owned by adapters.storage now, not the dispatcher.
    assert "dispatch_policies" not in tables

    columns = {row[1] for row in conn.execute("PRAGMA table_info(trigger_dispatches)")}
    assert columns == {"consumer", "source", "item_id"}


def test_ensure_tables_drops_old_next_due_at_or_last_dispatch_policy_schema():
    conn = _make_conn()
    conn.execute(
        "CREATE TABLE trigger_dispatches (consumer TEXT NOT NULL, source TEXT NOT NULL, "
        "item_id TEXT NOT NULL, times_triggered INTEGER NOT NULL DEFAULT 0, "
        "next_due_at TEXT NOT NULL, PRIMARY KEY (consumer, source, item_id))"
    )
    conn.execute(
        "INSERT INTO trigger_dispatches (consumer, source, item_id, next_due_at) "
        "VALUES ('log', 'csn', '1', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    _ensure_tables(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(trigger_dispatches)")}
    assert columns == {"consumer", "source", "item_id"}
    assert conn.execute("SELECT COUNT(*) FROM trigger_dispatches").fetchone()[0] == 0


def test_ensure_tables_drops_times_triggered_and_last_triggered_at_keeping_armed_rows():
    """The repeat-loop schema had times_triggered / last_triggered_at.
    Those are dropped in place — a currently-armed row survives and just
    delivers once more."""
    conn = _make_conn()
    conn.execute(
        "CREATE TABLE trigger_dispatches (consumer TEXT NOT NULL, source TEXT NOT NULL, "
        "item_id TEXT NOT NULL, times_triggered INTEGER NOT NULL DEFAULT 0, "
        "last_triggered_at TEXT, PRIMARY KEY (consumer, source, item_id))"
    )
    conn.execute(
        "INSERT INTO trigger_dispatches (consumer, source, item_id, times_triggered) "
        "VALUES ('log', 'csn', '1', 2)"
    )
    conn.commit()

    _ensure_tables(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(trigger_dispatches)")}
    assert columns == {"consumer", "source", "item_id"}
    assert _armed(conn, "log", "csn", "1")



def test_first_poll_skips_existing_backlog_but_records_watermark():
    conn = _make_conn()
    _insert_item(conn, "senapred", "1", "informational")
    _insert_item(conn, "senapred", "2", "urgent")

    discovered = discover_new_items(conn, "log")

    assert discovered == 0
    watermark = conn.execute(
        "SELECT last_seen_rowid FROM dispatcher_state WHERE consumer = ?", ("log",)
    ).fetchone()[0]
    assert watermark == 2


def test_discover_new_items_skips_arming_item_whose_source_date_time_predates_not_before():
    conn = _make_conn()
    discover_new_items(conn, "log")  # establish watermark before the item exists

    _insert_item(
        conn, "senapred", "1", "informational",
        source_date_time="2020-01-01T00:00:00+00:00",
    )

    discovered = discover_new_items(conn, "log", not_before=NOW)

    assert discovered == 1  # still counted as "seen"
    assert not _armed(conn, "log", "senapred", "1")
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'item.discovered_stale_skipped' "
        "AND source = 'senapred' AND item_id = '1'"
    ).fetchone() is not None


def test_discover_new_items_still_arms_item_with_source_date_time_at_or_after_not_before():
    conn = _make_conn()
    discover_new_items(conn, "log")

    _insert_item(
        conn, "senapred", "1", "informational",
        source_date_time=NOW.isoformat(),
    )

    discovered = discover_new_items(conn, "log", not_before=NOW)

    assert discovered == 1
    assert _armed(conn, "log", "senapred", "1")


def test_discover_new_items_arms_item_with_no_source_date_time_regardless_of_not_before():
    conn = _make_conn()
    discover_new_items(conn, "log")

    _insert_item(conn, "senapred", "1", "informational", source_date_time=None)

    discovered = discover_new_items(conn, "log", not_before=NOW)

    assert discovered == 1
    assert _armed(conn, "log", "senapred", "1")


def test_discover_new_items_arms_everything_when_not_before_omitted():
    conn = _make_conn()
    discover_new_items(conn, "log")

    _insert_item(
        conn, "senapred", "1", "informational",
        source_date_time="2020-01-01T00:00:00+00:00",
    )

    discovered = discover_new_items(conn, "log")  # not_before defaults to None

    assert discovered == 1
    assert _armed(conn, "log", "senapred", "1")


def test_item_delivered_once_then_retired():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)  # establish watermark before the item exists

    _insert_item(conn, "senapred", "1", "informational")

    delivered = []
    dispatched = check_for_new_items(conn, consumer, [delivered.append])
    assert dispatched == 1
    assert len(delivered) == 1
    assert not _armed(conn, consumer, "senapred", "1")

    dispatched_again = check_for_new_items(conn, consumer, [delivered.append])
    assert dispatched_again == 0
    assert len(delivered) == 1


def test_urgent_item_also_delivered_only_once_by_the_dispatcher():
    """The retransmit count for 'urgent' is applied by beacon/, not here —
    the dispatcher hands every item to its handlers exactly once."""
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)

    _insert_item(conn, "csn", "1", "urgent")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert check_for_new_items(conn, consumer, [delivered.append]) == 0
    assert len(delivered) == 1


def test_handler_exception_does_not_stop_other_handlers_or_rows():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)

    _insert_item(conn, "senapred", "a", "informational")
    _insert_item(conn, "senapred", "b", "informational")

    def failing_handler(row):
        raise RuntimeError("boom")

    calls = []

    def recording_handler(row):
        calls.append(row["item_id"])

    dispatched = check_for_new_items(conn, consumer, [failing_handler, recording_handler])

    assert dispatched == 2
    assert sorted(calls) == ["a", "b"]
    assert not _armed(conn, consumer, "senapred", "a")
    assert not _armed(conn, consumer, "senapred", "b")


def test_policy_change_fires_one_fresh_dispatch():
    """Reassigning an item's policy re-flows it through the
    pipeline exactly once — sync_policy_changes arms it, dispatch_due_items
    delivers and retires it."""
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)

    _insert_item(conn, "csn", "1", "informational")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 1

    conn.execute(
        "UPDATE items SET policy = 'urgent' WHERE source = 'csn' AND item_id = '1'"
    )
    conn.commit()

    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 2
    assert not _armed(conn, consumer, "csn", "1")

    # no further dispatches without another change
    assert check_for_new_items(conn, consumer, [delivered.append]) == 0
    assert len(delivered) == 2


def test_policy_change_on_backlog_item_is_detected_and_fires():
    """An item that predates this consumer entirely (rowid watermark always
    skips it) still reacts to a policy change — sync_policy_changes
    scans every row in `items`."""
    conn = _make_conn()
    _insert_item(conn, "senapred", "backlog-1", "informational")

    delivered = []
    assert check_for_new_items(conn, "log", [delivered.append]) == 0
    assert len(delivered) == 0

    conn.execute(
        "UPDATE items SET policy = 'urgent' "
        "WHERE source = 'senapred' AND item_id = 'backlog-1'"
    )
    conn.commit()

    assert check_for_new_items(conn, "log", [delivered.append]) == 1
    assert len(delivered) == 1
    assert delivered[0]["item_id"] == "backlog-1"


def test_policy_change_on_retired_item_is_detected_and_fires_without_rearm():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)

    _insert_item(conn, "csn", "1", "informational")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 1
    assert not _armed(conn, consumer, "csn", "1")  # retired

    conn.execute(
        "UPDATE items SET policy = 'urgent' WHERE source = 'csn' AND item_id = '1'"
    )
    conn.commit()

    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 2


def test_rearm_fires_one_more_delivery_under_the_same_policy():
    from dispatcher.override import rearm_item

    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "csn", "1", "urgent")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert not _armed(conn, consumer, "csn", "1")

    assert rearm_item(conn, consumer, "csn", "1") is True
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 2


def test_discover_new_items_records_audit_event():
    conn = _make_conn()
    discover_new_items(conn, "log")
    _insert_item(conn, "senapred", "1", "informational")

    discover_new_items(conn, "log")

    row = conn.execute(
        "SELECT event_type, actor, source, item_id FROM audit_log WHERE event_type = 'item.discovered'"
    ).fetchone()
    assert tuple(row) == ("item.discovered", "dispatcher.watcher", "senapred", "1")


def test_sync_policy_changes_records_audit_event_on_drift():
    conn = _make_conn()
    discover_new_items(conn, "log")
    _insert_item(conn, "csn", "1", "informational")
    discover_new_items(conn, "log")  # baseline the item's policy

    conn.execute(
        "UPDATE items SET policy = 'urgent' WHERE source = 'csn' AND item_id = '1'"
    )
    conn.commit()
    watcher_module.sync_policy_changes(conn, "log")

    row = conn.execute(
        "SELECT event_type, source, item_id, details "
        "FROM audit_log WHERE event_type = 'item.policy_drifted'"
    ).fetchone()
    assert row[0] == "item.policy_drifted"
    assert row[1] == "csn"
    assert row[2] == "1"
    assert '"old_policy": "informational"' in row[3]
    assert '"new_policy": "urgent"' in row[3]


def test_sync_policy_changes_is_null_safe_both_directions():
    """The drift query uses SQL's `IS NOT` (not `<>`), which is NULL-safe --
    a policy going from a real value to NULL, or from NULL to a real value,
    must still be detected as drift, just as Python's `!=` on None always
    was. `<>` would silently drop both of these comparisons."""
    conn = _make_conn()
    discover_new_items(conn, "log")

    _insert_item(conn, "csn", "null-to-value", policy=None)
    _insert_item(conn, "csn", "value-to-null", policy="informational")
    discover_new_items(conn, "log")  # baseline both

    conn.execute(
        "UPDATE items SET policy = 'urgent' WHERE source = 'csn' AND item_id = 'null-to-value'"
    )
    conn.execute(
        "UPDATE items SET policy = NULL WHERE source = 'csn' AND item_id = 'value-to-null'"
    )
    conn.commit()

    assert watcher_module.sync_policy_changes(conn, "log") == 2
    assert _armed(conn, "log", "csn", "null-to-value")
    assert _armed(conn, "log", "csn", "value-to-null")

    new_policy = dict(
        conn.execute(
            "SELECT item_id, policy FROM item_policy_state WHERE consumer = 'log' AND source = 'csn'"
        )
    )
    assert new_policy["null-to-value"] == "urgent"
    assert new_policy["value-to-null"] is None


def test_sync_policy_changes_does_not_rearm_when_null_policy_is_unchanged():
    """A NULL policy that stays NULL is not drift -- IS NOT (like Python's
    `!=` on two Nones) treats NULL == NULL as "no change", so a delivered
    item stays retired rather than being rearmed."""
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "csn", "1", policy=None)

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 1
    assert not _armed(conn, consumer, "csn", "1")  # retired after delivery

    assert watcher_module.sync_policy_changes(conn, consumer) == 0
    assert not _armed(conn, consumer, "csn", "1")


def test_sync_policy_changes_baselines_new_item_in_one_batch():
    """Never-seen items are baselined via one executemany + one commit, not
    individually -- and, per the "silently" contract, without an audit
    event or arming."""
    conn = _make_conn()
    discover_new_items(conn, "log")
    _insert_item(conn, "csn", "seed", policy="informational")
    discover_new_items(conn, "log")  # baseline "seed"; also creates audit_log

    _insert_item(conn, "csn", "a", policy="informational")
    _insert_item(conn, "csn", "b", policy=None)

    assert watcher_module.sync_policy_changes(conn, "log") == 0
    assert not _armed(conn, "log", "csn", "a")
    assert not _armed(conn, "log", "csn", "b")

    baseline = dict(
        conn.execute(
            "SELECT item_id, policy FROM item_policy_state WHERE consumer = 'log' AND source = 'csn'"
        )
    )
    assert baseline["a"] == "informational"
    assert baseline["b"] is None
    assert (
        conn.execute(
            "SELECT 1 FROM audit_log WHERE event_type = 'item.policy_drifted'"
        ).fetchone()
        is None
    )


def test_dispatch_due_items_records_audit_event_on_success():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "senapred", "1", "urgent")

    check_for_new_items(conn, consumer, [lambda row: None])

    row = conn.execute(
        "SELECT event_type, source, item_id, details "
        "FROM audit_log WHERE event_type = 'item.dispatched'"
    ).fetchone()
    assert tuple(row[:3]) == ("item.dispatched", "senapred", "1")
    assert '"policy": "urgent"' in row[3]
    assert "times_triggered" not in row[3]


def test_dispatch_due_items_records_audit_event_on_handler_failure():
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "senapred", "1", "informational")

    def failing_handler(row):
        raise RuntimeError("boom")

    check_for_new_items(conn, consumer, [failing_handler])

    row = conn.execute(
        "SELECT event_type, source, item_id, details "
        "FROM audit_log WHERE event_type = 'item.dispatch_failed'"
    ).fetchone()
    assert row[0] == "item.dispatch_failed"
    assert row[1] == "senapred"
    assert row[2] == "1"
    assert "boom" in row[3]
    # failure is still one-shot: the row is retired
    assert not _armed(conn, consumer, "senapred", "1")


def test_independent_consumers_track_separate_watermarks_and_schedules():
    conn = _make_conn()
    discover_new_items(conn, "consumer-a")
    discover_new_items(conn, "consumer-b")

    _insert_item(conn, "senapred", "1", "informational")

    delivered_a = []
    delivered_b = []
    check_for_new_items(conn, "consumer-a", [delivered_a.append])
    assert len(delivered_a) == 1
    assert len(delivered_b) == 0

    check_for_new_items(conn, "consumer-b", [delivered_b.append])
    assert len(delivered_b) == 1

    check_for_new_items(conn, "consumer-a", [delivered_a.append])
    assert len(delivered_a) == 1
