import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Callable

from adapters.storage import record_audit_event
from adapters.timeutil import utc_now as _now_dt

from . import policy as policy_module

logger = logging.getLogger(__name__)

Handler = Callable[[sqlite3.Row], None]

_CREATE_DISPATCHER_STATE = """
CREATE TABLE IF NOT EXISTS dispatcher_state (
    consumer TEXT PRIMARY KEY,
    last_seen_rowid INTEGER NOT NULL
);
"""

_CREATE_TRIGGER_DISPATCHES = """
CREATE TABLE IF NOT EXISTS trigger_dispatches (
    consumer TEXT NOT NULL,
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    times_triggered INTEGER NOT NULL DEFAULT 0,
    last_triggered_at TEXT,
    PRIMARY KEY (consumer, source, item_id)
);
"""

_CREATE_DISPATCH_POLICIES = """
CREATE TABLE IF NOT EXISTS dispatch_policies (
    name TEXT PRIMARY KEY,
    repeat_times INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    description TEXT
);
"""

_CREATE_ITEM_POLICY_STATE = """
CREATE TABLE IF NOT EXISTS item_policy_state (
    consumer TEXT NOT NULL,
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    dispatch_policy TEXT,
    PRIMARY KEY (consumer, source, item_id)
);
"""


def _migrate_trigger_state_table(conn: sqlite3.Connection) -> None:
    """trigger_state was renamed to dispatcher_state when the package that
    owns it (triggers/) was renamed to dispatcher/. Existing databases
    still have the old-named table with real watermark data in it — that
    data is the source of truth for what's already been discovered, so it
    gets renamed in place rather than dropped."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'trigger_state'"
    ).fetchone()
    if exists:
        conn.execute("ALTER TABLE trigger_state RENAME TO dispatcher_state")
        conn.commit()


def _migrate_trigger_dispatches_table(conn: sqlite3.Connection) -> None:
    """trigger_dispatches used to store an absolute next_due_at, computed
    once at schedule time — which meant a policy's interval_seconds
    change didn't move an already-scheduled item's due time until it
    next fired. Replaced by last_triggered_at, so due-ness is recomputed
    live against the *current* policy on every poll. There's no lossless
    conversion between the two (next_due_at doesn't reveal what interval
    produced it), but this table is disposable scheduling state, not a
    source of truth — items/dispatch_policies are — so an old-schema
    table is just dropped and recreated; any item mid-redelivery simply
    restarts its count from 0 on the next poll.

    A short-lived intermediate schema also had a last_dispatch_policy
    column here, tracking policy drift per in-flight row — replaced by
    the standalone item_policy_state table (below), which covers every
    item ever seen, not just ones currently in trigger_dispatches, so a
    policy change on an already-retired or never-discovered item is
    detected too. Dropped if present."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(trigger_dispatches)")}
    if "next_due_at" in columns:
        conn.execute("DROP TABLE trigger_dispatches")
        conn.commit()
        return

    if "last_dispatch_policy" in columns:
        conn.execute("ALTER TABLE trigger_dispatches DROP COLUMN last_dispatch_policy")
        conn.commit()


def _ensure_tables(conn: sqlite3.Connection) -> None:
    _migrate_trigger_state_table(conn)
    _migrate_trigger_dispatches_table(conn)
    conn.execute(_CREATE_DISPATCHER_STATE)
    conn.execute(_CREATE_TRIGGER_DISPATCHES)
    conn.execute(_CREATE_DISPATCH_POLICIES)
    conn.execute(_CREATE_ITEM_POLICY_STATE)
    conn.commit()
    policy_module.ensure_seeded(conn)


def _last_seen_rowid(conn: sqlite3.Connection, consumer: str) -> int:
    row = conn.execute(
        "SELECT last_seen_rowid FROM dispatcher_state WHERE consumer = ?", (consumer,)
    ).fetchone()
    if row is not None:
        return row[0]

    # First run for this consumer: skip the existing backlog instead of
    # scheduling every historical row for delivery — start the watermark
    # at the current max rowid. To intentionally replay history for this
    # consumer, delete its row from dispatcher_state (see README).
    max_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM items").fetchone()[0]
    conn.execute(
        "INSERT INTO dispatcher_state (consumer, last_seen_rowid) VALUES (?, ?)",
        (consumer, max_rowid),
    )
    conn.commit()
    return max_rowid


def _arm(conn: sqlite3.Connection, consumer: str, source: str, item_id: str) -> None:
    """Marks an item due right now: inserts a trigger_dispatches row if
    none exists, or resets an existing one's progress — either way,
    granting the item's *current* dispatch_policy a fresh delivery budget
    from scratch (see dispatch_due_items)."""
    conn.execute(
        "INSERT INTO trigger_dispatches "
        "(consumer, source, item_id, times_triggered, last_triggered_at) "
        "VALUES (?, ?, ?, 0, NULL) "
        "ON CONFLICT (consumer, source, item_id) "
        "DO UPDATE SET times_triggered = 0, last_triggered_at = NULL",
        (consumer, source, item_id),
    )


def discover_new_items(conn: sqlite3.Connection, consumer: str) -> int:
    """Finds rows inserted into `items` since this consumer last looked,
    and schedules each for immediate delivery (due now). Also records
    each one's current dispatch_policy as its baseline in
    item_policy_state, so sync_policy_changes doesn't treat this same
    item as newly-changed on the very next poll. Returns the number of
    rows discovered."""
    _ensure_tables(conn)
    last_seen = _last_seen_rowid(conn, consumer)

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT rowid, source, item_id, dispatch_policy FROM items "
        "WHERE rowid > ? ORDER BY rowid",
        (last_seen,),
    ).fetchall()

    for row in rows:
        _arm(conn, consumer, row["source"], row["item_id"])
        conn.execute(
            "INSERT OR REPLACE INTO item_policy_state "
            "(consumer, source, item_id, dispatch_policy) VALUES (?, ?, ?, ?)",
            (consumer, row["source"], row["item_id"], row["dispatch_policy"]),
        )
        conn.execute(
            "UPDATE dispatcher_state SET last_seen_rowid = ? WHERE consumer = ?",
            (row["rowid"], consumer),
        )
        conn.commit()
        record_audit_event(
            conn,
            event_type="item.discovered",
            actor="dispatcher.watcher",
            source=row["source"],
            item_id=row["item_id"],
            details={"consumer": consumer},
        )

    return len(rows)


def sync_policy_changes(conn: sqlite3.Connection, consumer: str) -> int:
    """Scans *every* row in `items` (not just ones currently tracked in
    trigger_dispatches) for a dispatch_policy that's drifted since this
    consumer last recorded it in item_policy_state, and arms any that
    have — regardless of whether the item is currently in-flight, already
    retired (its repeat budget was previously exhausted), or was never
    discovered as "new" at all (predates this consumer's watermark, i.e.
    backlog). This is what makes a dispatch_policy edit on *any* existing
    row reactive, not just ones already mid-delivery.

    An item with no recorded state yet (never checked before by this
    consumer — true for backlog items discover_new_items always skipped)
    just has its current value baselined, silently, the first time it's
    seen here — matching the same "don't flood on first run" principle
    as discover_new_items' watermark skip. Only a *change relative to
    that baseline* arms an item. Returns the number of items armed."""
    _ensure_tables(conn)
    conn.row_factory = sqlite3.Row

    # A dict lookup (vs. a LEFT JOIN) cleanly distinguishes "no snapshot
    # recorded yet" from "recorded, and it happens to be NULL" — an item
    # whose dispatch_policy is itself genuinely unset.
    known = {
        (row["source"], row["item_id"]): row["dispatch_policy"]
        for row in conn.execute(
            "SELECT source, item_id, dispatch_policy FROM item_policy_state "
            "WHERE consumer = ?",
            (consumer,),
        )
    }

    items = conn.execute("SELECT source, item_id, dispatch_policy FROM items").fetchall()

    changed = 0
    for row in items:
        key = (row["source"], row["item_id"])
        current = row["dispatch_policy"]

        if key not in known:
            # Never seen by this consumer before — baseline silently.
            conn.execute(
                "INSERT INTO item_policy_state "
                "(consumer, source, item_id, dispatch_policy) VALUES (?, ?, ?, ?)",
                (consumer, row["source"], row["item_id"], current),
            )
            conn.commit()
            continue

        if known[key] != current:
            _arm(conn, consumer, row["source"], row["item_id"])
            conn.execute(
                "UPDATE item_policy_state SET dispatch_policy = ? "
                "WHERE consumer = ? AND source = ? AND item_id = ?",
                (current, consumer, row["source"], row["item_id"]),
            )
            conn.commit()
            record_audit_event(
                conn,
                event_type="item.policy_drifted",
                actor="dispatcher.watcher",
                source=row["source"],
                item_id=row["item_id"],
                details={"consumer": consumer, "old_policy": known[key], "new_policy": current},
            )
            changed += 1

    return changed


def dispatch_due_items(
    conn: sqlite3.Connection, consumer: str, handlers: list[Handler]
) -> int:
    """Delivers every in-flight trigger_dispatches row for `consumer`
    that's currently due to every handler, then reschedules or retires
    each row per the RepeatPolicy its `dispatch_policy` name resolves to
    (see policy.policy_for and adapters.storage's store_reading
    docstring). Due-ness is computed live, every call, from
    `last_triggered_at + policy.interval_seconds` using each row's
    *current* policy — never a precomputed timestamp — so editing a
    policy's own repeat_times/interval_seconds (e.g. via
    dispatcher/policies.py) takes effect immediately: a shortened interval
    makes an already-scheduled item due sooner, a lengthened one pushes
    it out, both without waiting for the item's old schedule to elapse
    first. (A dispatch_policy change on the item itself is handled
    earlier, by sync_policy_changes forcing last_triggered_at back to
    NULL — by the time this function runs, that just looks like an item
    that's due.) Returns the number of rows dispatched."""
    conn.row_factory = sqlite3.Row
    in_flight = conn.execute(
        "SELECT d.source, d.item_id, d.times_triggered, d.last_triggered_at, i.* "
        "FROM trigger_dispatches d "
        "JOIN items i ON i.source = d.source AND i.item_id = d.item_id "
        "WHERE d.consumer = ?",
        (consumer,),
    ).fetchall()

    now = _now_dt()
    dispatched = 0
    for row in in_flight:
        policy = policy_module.policy_for(conn, row["dispatch_policy"])
        if row["last_triggered_at"] is not None:
            due_at = datetime.fromisoformat(row["last_triggered_at"]) + timedelta(
                seconds=policy.interval_seconds
            )
            if now < due_at:
                continue  # not due yet, under the current policy

        for handler in handlers:
            handler_name = getattr(handler, "__name__", str(handler))
            try:
                handler(row)
            except Exception as exc:
                logger.error(
                    "handler %s failed for source=%s item_id=%s",
                    handler_name,
                    row["source"],
                    row["item_id"],
                    exc_info=True,
                )
                # Best-effort: a failure recording the audit row itself must
                # never mask the original handler exception above.
                try:
                    record_audit_event(
                        conn,
                        event_type="item.dispatch_failed",
                        actor=handler_name,
                        source=row["source"],
                        item_id=row["item_id"],
                        details={
                            "consumer": consumer,
                            "dispatch_policy": row["dispatch_policy"],
                            "error": str(exc),
                        },
                    )
                except Exception:
                    logger.error("failed to record audit event for dispatch failure", exc_info=True)
            else:
                try:
                    record_audit_event(
                        conn,
                        event_type="item.dispatched",
                        actor=handler_name,
                        source=row["source"],
                        item_id=row["item_id"],
                        details={
                            "consumer": consumer,
                            "dispatch_policy": row["dispatch_policy"],
                            "times_triggered": row["times_triggered"] + 1,
                        },
                    )
                except Exception:
                    logger.error("failed to record audit event for dispatch success", exc_info=True)

        times_triggered = row["times_triggered"] + 1
        if times_triggered < policy.repeat_times:
            conn.execute(
                "UPDATE trigger_dispatches SET times_triggered = ?, last_triggered_at = ? "
                "WHERE consumer = ? AND source = ? AND item_id = ?",
                (times_triggered, now.isoformat(), consumer, row["source"], row["item_id"]),
            )
        else:
            conn.execute(
                "DELETE FROM trigger_dispatches "
                "WHERE consumer = ? AND source = ? AND item_id = ?",
                (consumer, row["source"], row["item_id"]),
            )
        conn.commit()
        dispatched += 1

    return dispatched


def check_for_new_items(
    conn: sqlite3.Connection, consumer: str, handlers: list[Handler]
) -> int:
    """The poll loop's single entry point:
    1. discover_new_items — brand-new rows since this consumer last looked.
    2. sync_policy_changes — any row (new, in-flight, retired, or backlog)
       whose dispatch_policy has drifted since last recorded.
    3. dispatch_due_items — delivers everything currently due.
    Returns the number of rows dispatched this call."""
    discover_new_items(conn, consumer)
    sync_policy_changes(conn, consumer)
    return dispatch_due_items(conn, consumer, handlers)


def log_handler(row: sqlite3.Row) -> None:
    """The one built-in handler — logs the item. Real handlers (radio TX,
    etc.) register alongside/instead of this in dispatcher.__main__."""
    logger.info(
        "trigger: source=%s item_id=%s type=%s dispatch_policy=%s title=%r url=%s",
        row["source"],
        row["item_id"],
        row["type"],
        row["dispatch_policy"],
        row["extracted_title"],
        row["url"],
    )
