import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import dispatcher.watcher as watcher_module
from dispatcher.policy import set_policy
from dispatcher.watcher import _ensure_tables, check_for_new_items, discover_new_items


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE items ("
        "source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "extracted_title TEXT, url TEXT, type TEXT, dispatch_policy TEXT, "
        "source_date_time TEXT, "
        "PRIMARY KEY (source, item_id))"
    )
    return conn


def _insert_item(
    conn,
    source,
    item_id,
    dispatch_policy,
    title="Title",
    url="http://example.test",
    source_date_time=None,
):
    conn.execute(
        "INSERT INTO items "
        "(source, item_id, extracted_title, url, type, dispatch_policy, source_date_time) "
        "VALUES (?, ?, ?, ?, 'Type', ?, ?)",
        (source, item_id, title, url, dispatch_policy, source_date_time),
    )
    conn.commit()


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(watcher_module, "_now_dt", fake)
    return fake


def test_ensure_tables_creates_and_seeds_tables_idempotently():
    conn = _make_conn()

    _ensure_tables(conn)
    _ensure_tables(conn)  # idempotent, no error, no re-seeding over edits

    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "dispatcher_state" in tables
    assert "trigger_dispatches" in tables
    assert "dispatch_policies" in tables

    policy_names = {
        row[0] for row in conn.execute("SELECT name FROM dispatch_policies")
    }
    assert policy_names == {"urgent", "informational"}


def test_ensure_tables_migrates_old_next_due_at_schema():
    """Covers a trigger_dispatches table from before last_triggered_at
    existed (the old absolute next_due_at column) — dropped and
    recreated on the new schema, since there's no lossless conversion
    and it's disposable scheduling state, not a source of truth."""
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
    assert "next_due_at" not in columns
    assert "last_triggered_at" in columns
    count = conn.execute("SELECT COUNT(*) FROM trigger_dispatches").fetchone()[0]
    assert count == 0


def test_ensure_tables_drops_last_dispatch_policy_column():
    """Covers a trigger_dispatches table from the short-lived intermediate
    schema that tracked policy drift per in-flight row via
    last_dispatch_policy — superseded by the standalone item_policy_state
    table (which covers every item, not just in-flight ones), so the
    column is dropped. Existing progress on other columns is preserved."""
    conn = _make_conn()
    conn.execute(
        "CREATE TABLE trigger_dispatches (consumer TEXT NOT NULL, source TEXT NOT NULL, "
        "item_id TEXT NOT NULL, times_triggered INTEGER NOT NULL DEFAULT 0, "
        "last_triggered_at TEXT, last_dispatch_policy TEXT, "
        "PRIMARY KEY (consumer, source, item_id))"
    )
    conn.execute(
        "INSERT INTO trigger_dispatches "
        "(consumer, source, item_id, times_triggered, last_dispatch_policy) "
        "VALUES ('log', 'csn', '1', 2, 'urgent')"
    )
    conn.commit()

    _ensure_tables(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(trigger_dispatches)")}
    assert "last_dispatch_policy" not in columns
    row = conn.execute(
        "SELECT times_triggered FROM trigger_dispatches "
        "WHERE consumer = 'log' AND source = 'csn' AND item_id = '1'"
    ).fetchone()
    assert row == (2,)  # existing progress preserved


def test_ensure_tables_creates_item_policy_state_table():
    conn = _make_conn()

    _ensure_tables(conn)

    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "item_policy_state" in tables


def test_first_poll_skips_existing_backlog_but_records_watermark(clock):
    conn = _make_conn()
    _insert_item(conn, "senapred", "1", "informational")
    _insert_item(conn, "senapred", "2", "urgent")

    discovered = discover_new_items(conn, "log")

    assert discovered == 0
    watermark = conn.execute(
        "SELECT last_seen_rowid FROM dispatcher_state WHERE consumer = ?", ("log",)
    ).fetchone()[0]
    assert watermark == 2


def test_discover_new_items_skips_arming_item_whose_source_date_time_predates_not_before(clock):
    """The fix for a first-ever adapter poll flooding delivery: a fresh
    fetch can return a batch of already-old real-world events (a backlog
    of past earthquakes/alerts, not new ones) in one response. Each is a
    brand-new `items` row from the watermark's point of view, but its own
    source_date_time is stale relative to not_before (this process's own
    startup instant) -- it must never be armed for dispatch."""
    conn = _make_conn()
    discover_new_items(conn, "log")  # establish watermark before the item exists

    _insert_item(
        conn, "senapred", "1", "informational",
        source_date_time="2020-01-01T00:00:00+00:00",  # long before "now" below
    )

    discovered = discover_new_items(conn, "log", not_before=clock.now)

    assert discovered == 1  # still counted as "seen"
    assert conn.execute(
        "SELECT 1 FROM trigger_dispatches WHERE consumer = 'log' "
        "AND source = 'senapred' AND item_id = '1'"
    ).fetchone() is None
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'item.discovered_stale_skipped' "
        "AND source = 'senapred' AND item_id = '1'"
    ).fetchone()
    assert row is not None


def test_discover_new_items_still_arms_item_with_source_date_time_at_or_after_not_before(clock):
    conn = _make_conn()
    discover_new_items(conn, "log")

    _insert_item(
        conn, "senapred", "1", "informational",
        source_date_time=clock.now.isoformat(),  # right at not_before, not before it
    )

    discovered = discover_new_items(conn, "log", not_before=clock.now)

    assert discovered == 1
    assert conn.execute(
        "SELECT 1 FROM trigger_dispatches WHERE consumer = 'log' "
        "AND source = 'senapred' AND item_id = '1'"
    ).fetchone() is not None


def test_discover_new_items_arms_item_with_no_source_date_time_regardless_of_not_before(clock):
    """A missing source_date_time isn't treated as "infinitely stale" --
    without a real value to compare, there's nothing to filter on."""
    conn = _make_conn()
    discover_new_items(conn, "log")

    _insert_item(conn, "senapred", "1", "informational", source_date_time=None)

    discovered = discover_new_items(conn, "log", not_before=clock.now)

    assert discovered == 1
    assert conn.execute(
        "SELECT 1 FROM trigger_dispatches WHERE consumer = 'log' "
        "AND source = 'senapred' AND item_id = '1'"
    ).fetchone() is not None


def test_discover_new_items_arms_everything_when_not_before_omitted(clock):
    conn = _make_conn()
    discover_new_items(conn, "log")

    _insert_item(
        conn, "senapred", "1", "informational",
        source_date_time="2020-01-01T00:00:00+00:00",
    )

    discovered = discover_new_items(conn, "log")  # not_before defaults to None

    assert discovered == 1
    assert conn.execute(
        "SELECT 1 FROM trigger_dispatches WHERE consumer = 'log' "
        "AND source = 'senapred' AND item_id = '1'"
    ).fetchone() is not None


def test_informational_item_delivered_once(clock):
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)  # establish watermark before the item exists

    _insert_item(conn, "senapred", "1", "informational")

    delivered = []
    dispatched = check_for_new_items(conn, consumer, [delivered.append])
    assert dispatched == 1
    assert len(delivered) == 1

    dispatched_again = check_for_new_items(conn, consumer, [delivered.append])
    assert dispatched_again == 0
    assert len(delivered) == 1


def test_urgent_item_redelivered_after_interval_elapsed(clock):
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)  # creates + seeds dispatch_policies
    set_policy(conn, "urgent", repeat_times=3, interval_seconds=60)

    _insert_item(conn, "csn", "2026-01-01T00:00:00", "urgent")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 1

    clock.advance(10)  # not yet due (interval is 60s)
    assert check_for_new_items(conn, consumer, [delivered.append]) == 0
    assert len(delivered) == 1

    clock.advance(55)  # 65s since first delivery: due
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 2

    clock.advance(60)  # third and final delivery (repeat_times=3)
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 3

    clock.advance(1000)  # budget exhausted, no more deliveries
    assert check_for_new_items(conn, consumer, [delivered.append]) == 0
    assert len(delivered) == 3

    remaining = conn.execute(
        "SELECT COUNT(*) FROM trigger_dispatches WHERE consumer = ?", (consumer,)
    ).fetchone()[0]
    assert remaining == 0


def test_handler_exception_does_not_stop_other_handlers_or_rows(clock):
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


def test_different_items_pointing_at_different_named_policies_get_different_treatment(clock):
    """The whole point of dispatch_policy: per-item nuance without any
    override-column machinery — two CSN-like items with the same
    "urgent"-ish intent can point at different named policies."""
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    set_policy(conn, "no-repeat", repeat_times=1, interval_seconds=0)
    set_policy(conn, "urgent", repeat_times=3, interval_seconds=60)

    _insert_item(conn, "csn", "minor", "no-repeat")
    _insert_item(conn, "csn", "major", "urgent")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 2
    assert len(delivered) == 2

    clock.advance(60)
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1  # only "major" again
    assert len(delivered) == 3
    assert delivered[-1]["item_id"] == "major"


def test_shortening_policy_interval_makes_inflight_item_due_sooner(clock):
    """Due-ness is computed live from last_triggered_at + the policy's
    *current* interval_seconds, not a timestamp precomputed at schedule
    time — so editing dispatch_policies (e.g. via dispatcher/policies.py)
    affects an already-scheduled item immediately, without waiting for
    its old interval to elapse first."""
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    set_policy(conn, "urgent", repeat_times=5, interval_seconds=60)

    _insert_item(conn, "csn", "1", "urgent")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 1

    clock.advance(5)  # nowhere near the original 60s interval
    assert check_for_new_items(conn, consumer, [delivered.append]) == 0
    assert len(delivered) == 1

    # shorten the interval to 5s — should make the item due right away,
    # even though only 5s have passed since the first delivery
    set_policy(conn, "urgent", repeat_times=5, interval_seconds=5)
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 2


def test_lengthening_policy_interval_delays_inflight_item(clock):
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    set_policy(conn, "urgent", repeat_times=5, interval_seconds=5)

    _insert_item(conn, "csn", "1", "urgent")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 1

    # lengthen the interval before the original 5s elapses
    set_policy(conn, "urgent", repeat_times=5, interval_seconds=120)

    clock.advance(5)  # would have been due under the old 5s interval
    assert check_for_new_items(conn, consumer, [delivered.append]) == 0
    assert len(delivered) == 1

    clock.advance(115)  # now 120s have passed since the first delivery
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 2


def test_dispatch_policy_change_fires_immediately_without_waiting_for_interval(clock):
    """The distinct new behavior: reassigning an in-flight item's
    dispatch_policy is itself treated as "due now", not just a change to
    future scheduling — proven here with zero elapsed time (well inside
    the old policy's interval), unlike a test that happens to also wait
    out the old interval."""
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    set_policy(conn, "urgent", repeat_times=5, interval_seconds=300)  # long interval

    _insert_item(conn, "csn", "1", "urgent")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 1

    # No time advanced at all — under the old policy this item wouldn't
    # be due for another 300s. Reassign its policy directly on items.
    conn.execute(
        "UPDATE items SET dispatch_policy = 'informational' WHERE source = 'csn' AND item_id = '1'"
    )
    conn.commit()

    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 2  # fired immediately, purely from the policy reassignment

    # informational's repeat_times=1 — the reassignment restarted the
    # count under the new policy, so this one delivery already retires it
    remaining = conn.execute(
        "SELECT COUNT(*) FROM trigger_dispatches WHERE consumer = ?", (consumer,)
    ).fetchone()[0]
    assert remaining == 0


def test_dispatch_policy_reassignment_restarts_delivery_count_under_new_policy(clock):
    """Switching policies grants the new policy's full repeat_times
    budget from scratch, rather than continuing to count down whatever
    was accumulated under the old policy."""
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    set_policy(conn, "urgent", repeat_times=2, interval_seconds=0)
    set_policy(conn, "mega", repeat_times=3, interval_seconds=0)

    _insert_item(conn, "csn", "1", "urgent")

    delivered = []
    # deliver twice under "urgent" (repeat_times=2) — right up to retirement
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 2
    remaining = conn.execute(
        "SELECT times_triggered FROM trigger_dispatches WHERE consumer = ? AND item_id = '1'",
        (consumer,),
    ).fetchone()
    assert remaining is None  # already retired

    # re-arm and reassign to a fresh policy with its own 3-delivery budget
    from dispatcher.override import rearm_item

    rearm_item(conn, consumer, "csn", "1")
    conn.execute("UPDATE items SET dispatch_policy = 'mega' WHERE source = 'csn' AND item_id = '1'")
    conn.commit()

    for _ in range(3):
        assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 5  # 2 under "urgent" + 3 under "mega"
    remaining = conn.execute(
        "SELECT COUNT(*) FROM trigger_dispatches WHERE consumer = ?", (consumer,)
    ).fetchone()[0]
    assert remaining == 0


def test_manual_dispatch_policy_edit_takes_effect_on_next_poll_for_inflight_item(clock):
    """Simulates an override made directly on `items` (e.g. via
    dispatcher/override_item.py or a future UI) while the item is still
    in-flight — the reschedule-or-retire decision made at the next due
    poll uses the new dispatch_policy, with no extra step needed (a
    delivery already due at that exact poll still fires — the edit
    affects whether *more* follow up, not one already in flight)."""
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    set_policy(conn, "urgent", repeat_times=5, interval_seconds=10)

    _insert_item(conn, "csn", "1", "urgent")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 1

    # downgrade directly on items — not via any dispatcher API
    conn.execute(
        "UPDATE items SET dispatch_policy = 'informational' WHERE source = 'csn' AND item_id = '1'"
    )
    conn.commit()

    clock.advance(10)  # already-scheduled second delivery becomes due
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 2  # this one still fires — it was already due

    remaining = conn.execute(
        "SELECT COUNT(*) FROM trigger_dispatches WHERE consumer = ?", (consumer,)
    ).fetchone()[0]
    assert remaining == 0  # informational's repeat_times=1 already reached — retired

    clock.advance(1000)  # would have been due again 3 more times under "urgent"
    assert check_for_new_items(conn, consumer, [delivered.append]) == 0
    assert len(delivered) == 2


def test_policy_change_on_backlog_item_is_detected_and_fires(clock):
    """The new capability: an item that predates this consumer entirely
    (inserted before it ever polled, so discover_new_items' rowid
    watermark always skips it) still reacts to a dispatch_policy change
    — sync_policy_changes scans every row in `items`, not just newly
    discovered ones."""
    conn = _make_conn()
    _insert_item(conn, "senapred", "backlog-1", "informational")

    # first-ever poll: backlog skip applies (both to discovery and to
    # policy-state baselining) — nothing fires
    delivered = []
    assert check_for_new_items(conn, "log", [delivered.append]) == 0
    assert len(delivered) == 0

    # change the policy directly — no adapter, no override_item.py, just a
    # raw edit to items, exactly like the plain UPDATE a UI would issue
    conn.execute(
        "UPDATE items SET dispatch_policy = 'urgent' "
        "WHERE source = 'senapred' AND item_id = 'backlog-1'"
    )
    conn.commit()

    assert check_for_new_items(conn, "log", [delivered.append]) == 1
    assert len(delivered) == 1
    assert delivered[0]["item_id"] == "backlog-1"


def test_policy_change_on_retired_item_is_detected_and_fires_without_rearm(clock):
    """The other half of the new capability: an item that already
    finished its whole delivery lifecycle (trigger_dispatches row gone)
    resumes delivery on a dispatch_policy change alone — no explicit
    --rearm needed."""
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)

    _insert_item(conn, "csn", "1", "informational")

    delivered = []
    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 1
    remaining = conn.execute(
        "SELECT COUNT(*) FROM trigger_dispatches WHERE consumer = ?", (consumer,)
    ).fetchone()[0]
    assert remaining == 0  # fully retired, informational's repeat_times=1

    conn.execute(
        "UPDATE items SET dispatch_policy = 'urgent' WHERE source = 'csn' AND item_id = '1'"
    )
    conn.commit()

    assert check_for_new_items(conn, consumer, [delivered.append]) == 1
    assert len(delivered) == 2


def test_discover_new_items_records_audit_event(clock):
    conn = _make_conn()
    discover_new_items(conn, "log")  # establish watermark before the item exists
    _insert_item(conn, "senapred", "1", "informational")

    discover_new_items(conn, "log")

    row = conn.execute(
        "SELECT event_type, actor, source, item_id FROM audit_log WHERE event_type = 'item.discovered'"
    ).fetchone()
    assert tuple(row) == ("item.discovered", "dispatcher.watcher", "senapred", "1")


def test_sync_policy_changes_records_audit_event_on_drift(clock):
    conn = _make_conn()
    discover_new_items(conn, "log")
    _insert_item(conn, "csn", "1", "informational")
    discover_new_items(conn, "log")  # baseline the item's policy

    conn.execute(
        "UPDATE items SET dispatch_policy = 'urgent' WHERE source = 'csn' AND item_id = '1'"
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


def test_dispatch_due_items_records_audit_event_on_success(clock):
    conn = _make_conn()
    consumer = "log"
    discover_new_items(conn, consumer)
    _insert_item(conn, "senapred", "1", "informational")

    check_for_new_items(conn, consumer, [lambda row: None])

    row = conn.execute(
        "SELECT event_type, source, item_id FROM audit_log WHERE event_type = 'item.dispatched'"
    ).fetchone()
    assert tuple(row) == ("item.dispatched", "senapred", "1")


def test_dispatch_due_items_records_audit_event_on_handler_failure(clock):
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


def test_independent_consumers_track_separate_watermarks_and_schedules(clock):
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
