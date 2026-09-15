import logging
import sqlite3
from datetime import datetime
from typing import Callable

from adapters.storage import _ensure_bootstrapped, record_audit_event

logger = logging.getLogger(__name__)

Handler = Callable[[sqlite3.Row], None]

_CREATE_DISPATCHER_STATE = """
CREATE TABLE IF NOT EXISTS dispatcher_state (
    consumer TEXT PRIMARY KEY,
    last_seen_rowid INTEGER NOT NULL
);
"""

# Just a "this item is armed for one delivery" marker now — the repeat
# count / interval that used to live here (times_triggered,
# last_triggered_at) moved into beacon/'s beacon_tx_schedule when the
# retransmit concept moved to the transmit layer. The dispatcher delivers
# each armed item to its handlers exactly once, then deletes the row.
_CREATE_TRIGGER_DISPATCHES = """
CREATE TABLE IF NOT EXISTS trigger_dispatches (
    consumer TEXT NOT NULL,
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    PRIMARY KEY (consumer, source, item_id)
);
"""

_CREATE_ITEM_POLICY_STATE = """
CREATE TABLE IF NOT EXISTS item_policy_state (
    consumer TEXT NOT NULL,
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    policy TEXT,
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
    """trigger_dispatches is disposable scheduling state, not a source of
    truth (items / policies / beacon_tx_schedule are), so an
    old-schema table is just dropped and recreated.

    - A very old schema stored an absolute next_due_at, and a short-lived
      one a last_dispatch_policy column — the whole table is dropped if
      either is present.
    - The repeat-loop schema had times_triggered / last_triggered_at.
      Those columns are dropped in place (keeping any currently-armed rows,
      which then deliver once more and retire) now that the dispatcher
      delivers each item exactly once and the retransmit count lives in
      beacon/."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(trigger_dispatches)")}
    if not columns:
        return
    if "next_due_at" in columns or "last_dispatch_policy" in columns:
        conn.execute("DROP TABLE trigger_dispatches")
        conn.commit()
        return

    for legacy_column in ("times_triggered", "last_triggered_at"):
        if legacy_column in columns:
            try:
                conn.execute(
                    f"ALTER TABLE trigger_dispatches DROP COLUMN {legacy_column}"
                )
                conn.commit()
            except sqlite3.OperationalError:
                logger.debug("trigger_dispatches.%s already dropped", legacy_column)


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Routes through adapters.storage's process-wide bootstrap cache
    (kind="dispatcher") — this is called on every UI HTTP request via
    ui.db.get_db, and used to independently re-run these migrations and
    CREATE TABLE statements every single call."""
    def _create(c: sqlite3.Connection) -> None:
        _migrate_trigger_state_table(c)
        _migrate_trigger_dispatches_table(c)
        c.execute(_CREATE_DISPATCHER_STATE)
        c.execute(_CREATE_TRIGGER_DISPATCHES)
        c.execute(_CREATE_ITEM_POLICY_STATE)
        c.commit()

    _ensure_bootstrapped(conn, "dispatcher", _create)


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
    """Marks an item armed for one delivery on the next poll. A no-op if
    it's already armed (dispatch_due_items delivers and retires it either
    way)."""
    conn.execute(
        "INSERT INTO trigger_dispatches (consumer, source, item_id) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (consumer, source, item_id) DO NOTHING",
        (consumer, source, item_id),
    )


def discover_new_items(
    conn: sqlite3.Connection, consumer: str, *, not_before: datetime | None = None
) -> int:
    """Finds rows inserted into `items` since this consumer last looked,
    and schedules each for immediate delivery (due now) — unless
    `not_before` is given and the row's own `source_date_time` (the
    event's real-world timestamp, not when this pipeline happened to
    fetch/insert it) predates it, in which case it's recorded as seen
    but never armed for dispatch.

    This is what stops an adapter's first-ever poll from flooding
    delivery: a fresh CSN/SENAPRED fetch can return a batch of
    already-old real-world events (a backlog of past earthquakes/alerts,
    not new ones) in one response — each lands as a brand-new `items`
    row (rowid > last_seen), but its source_date_time is old. Filtering
    here, at discovery — the earliest point an item could ever be
    dispatched — means a stale item never enters trigger_dispatches at
    all, rather than being caught later by some downstream reconciler.
    `not_before` is meant to be this process's own startup instant
    (passed in from __main__.py, captured once) — not a rolling
    max-age, so a genuinely new event is never excluded no matter how
    long this process has been running since.

    Also records each row's current policy as its baseline in
    item_policy_state (armed or not), so sync_policy_changes doesn't
    treat this same item as newly-changed on the very next poll.
    Returns the number of rows discovered (armed or skipped as stale)."""
    _ensure_tables(conn)
    last_seen = _last_seen_rowid(conn, consumer)

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT rowid, source, item_id, policy, source_date_time FROM items "
        "WHERE rowid > ? ORDER BY rowid",
        (last_seen,),
    ).fetchall()

    for row in rows:
        stale = False
        if not_before is not None and row["source_date_time"]:
            try:
                event_dt = datetime.fromisoformat(row["source_date_time"])
            except ValueError:
                event_dt = None
            stale = event_dt is not None and event_dt < not_before

        if stale:
            record_audit_event(
                conn,
                event_type="item.discovered_stale_skipped",
                actor="dispatcher.watcher",
                source=row["source"],
                item_id=row["item_id"],
                details={"consumer": consumer, "source_date_time": row["source_date_time"]},
            )
        else:
            _arm(conn, consumer, row["source"], row["item_id"])
            record_audit_event(
                conn,
                event_type="item.discovered",
                actor="dispatcher.watcher",
                source=row["source"],
                item_id=row["item_id"],
                details={"consumer": consumer},
            )

        conn.execute(
            "INSERT OR REPLACE INTO item_policy_state "
            "(consumer, source, item_id, policy) VALUES (?, ?, ?, ?)",
            (consumer, row["source"], row["item_id"], row["policy"]),
        )
        conn.execute(
            "UPDATE dispatcher_state SET last_seen_rowid = ? WHERE consumer = ?",
            (row["rowid"], consumer),
        )
        conn.commit()

    return len(rows)


def sync_policy_changes(conn: sqlite3.Connection, consumer: str) -> int:
    """Scans *every* row in `items` (not just ones currently tracked in
    trigger_dispatches) for a policy that's drifted since this
    consumer last recorded it in item_policy_state, and arms any that
    have — regardless of whether the item is currently in-flight, already
    retired, or was never discovered as "new" at all (predates this
    consumer's watermark, i.e. backlog). This is what makes a
    policy edit on *any* existing row re-flow through the pipeline
    (one fresh dispatch -> actions -> content_ready -> beacon picks up the
    new tier), not just ones already mid-delivery.

    An item with no recorded state yet (never checked before by this
    consumer — true for backlog items discover_new_items always skipped)
    just has its current value baselined, silently, the first time it's
    seen here — matching the same "don't flood on first run" principle
    as discover_new_items' watermark skip. Only a *change relative to
    that baseline* arms an item. Returns the number of items armed.

    Rather than pulling all of `items` and all of `item_policy_state` into
    Python and diffing dict-by-dict (an O(all rows) scan every tick,
    regardless of how few actually need action), two targeted queries ask
    SQLite for exactly the rows that matter:
    - a LEFT JOIN for "no item_policy_state row at all yet" (the baseline
      case — s.source IS NULL is only possible via a LEFT JOIN miss);
    - an INNER JOIN with `IS NOT` for "recorded, and different now" — SQL's
      NULL-safe comparison operator, matching Python's `!=` on None (unlike
      `<>`/`!=` in SQL, which is NOT NULL-safe and would silently drop a
      comparison where either side is NULL)."""
    _ensure_tables(conn)
    conn.row_factory = sqlite3.Row

    never_seen = conn.execute(
        """
        SELECT i.source, i.item_id, i.policy
        FROM items i
        LEFT JOIN item_policy_state s
               ON s.consumer = ? AND s.source = i.source AND s.item_id = i.item_id
        WHERE s.source IS NULL
        """,
        (consumer,),
    ).fetchall()

    # Never seen by this consumer before — baseline silently, no audit
    # event. Batched into one executemany + one commit since none of these
    # are individually significant (unlike an armed drift below).
    if never_seen:
        conn.executemany(
            "INSERT INTO item_policy_state "
            "(consumer, source, item_id, policy) VALUES (?, ?, ?, ?)",
            [(consumer, row["source"], row["item_id"], row["policy"]) for row in never_seen],
        )
        conn.commit()

    drifted = conn.execute(
        """
        SELECT i.source, i.item_id, i.policy AS new_policy, s.policy AS old_policy
        FROM items i
        JOIN item_policy_state s
             ON s.consumer = ? AND s.source = i.source AND s.item_id = i.item_id
        WHERE i.policy IS NOT s.policy
        """,
        (consumer,),
    ).fetchall()

    changed = 0
    for row in drifted:
        source, item_id = row["source"], row["item_id"]
        current, old_policy = row["new_policy"], row["old_policy"]

        _arm(conn, consumer, source, item_id)
        conn.execute(
            "UPDATE item_policy_state SET policy = ? "
            "WHERE consumer = ? AND source = ? AND item_id = ?",
            (current, consumer, source, item_id),
        )
        conn.commit()
        record_audit_event(
            conn,
            event_type="item.policy_drifted",
            actor="dispatcher.watcher",
            source=source,
            item_id=item_id,
            details={"consumer": consumer, "old_policy": old_policy, "new_policy": current},
        )
        changed += 1

    return changed


def dispatch_due_items(
    conn: sqlite3.Connection, consumer: str, handlers: list[Handler]
) -> int:
    """Delivers every armed trigger_dispatches row for `consumer` to every
    handler exactly once, then retires the row — success or failure. The
    "put it on air N times, spaced out" concern that used to live here
    (transmit count / interval / cron) now belongs to beacon/'s
    beacon_tx_schedule, downstream of content preparation; the dispatcher
    just decides *when an item goes live once*. A genuine re-send is a
    fresh arm (a policy change caught by sync_policy_changes, or
    an explicit override.rearm_item). Returns the number of rows
    dispatched."""
    conn.row_factory = sqlite3.Row
    armed = conn.execute(
        "SELECT d.source, d.item_id, i.* "
        "FROM trigger_dispatches d "
        "JOIN items i ON i.source = d.source AND i.item_id = d.item_id "
        "WHERE d.consumer = ?",
        (consumer,),
    ).fetchall()

    dispatched = 0
    for row in armed:
        for handler in handlers:
            handler_name = getattr(handler, "__name__", str(handler))
            try:
                handler(row)
            except Exception as exc:
                logger.error(
                    "handler failed handler=%s source=%s item_id=%s",
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
                            "policy": row["policy"],
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
                            "policy": row["policy"],
                        },
                    )
                except Exception:
                    logger.error("failed to record audit event for dispatch success", exc_info=True)

        conn.execute(
            "DELETE FROM trigger_dispatches "
            "WHERE consumer = ? AND source = ? AND item_id = ?",
            (consumer, row["source"], row["item_id"]),
        )
        conn.commit()
        dispatched += 1

    return dispatched


def check_for_new_items(
    conn: sqlite3.Connection,
    consumer: str,
    handlers: list[Handler],
    *,
    not_before: datetime | None = None,
) -> int:
    """The poll loop's single entry point:
    1. discover_new_items — brand-new rows since this consumer last looked
       (skips arming any whose source_date_time predates `not_before` —
       see its docstring).
    2. sync_policy_changes — any row (new, armed, retired, or backlog)
       whose policy has drifted since last recorded.
    3. dispatch_due_items — delivers every armed item once and retires it.
    Returns the number of rows dispatched this call."""
    discover_new_items(conn, consumer, not_before=not_before)
    sync_policy_changes(conn, consumer)
    return dispatch_due_items(conn, consumer, handlers)


def log_handler(row: sqlite3.Row) -> None:
    """The one built-in handler — logs the item. Real handlers (radio TX,
    etc.) register alongside/instead of this in dispatcher.__main__."""
    logger.info(
        "dispatched item source=%s item_id=%s type=%s policy=%s title=%r url=%s",
        row["source"],
        row["item_id"],
        row["type"],
        row["policy"],
        row["extracted_title"],
        row["url"],
    )
