import sqlite3

from adapters.storage import record_audit_event

from .watcher import _arm, _ensure_tables


def override_item(
    conn: sqlite3.Connection,
    source: str,
    item_id: str,
    *,
    policy: str | None = None,
) -> bool:
    """Re-points an item at a different whole Policy (a row in `policies`
    — see adapters.policy). `items.policy` is the sanctioned exception to
    items' adapter-side immutability (see adapters.storage.store_reading's
    docstring) — a human/UI may update it after the fact. On the very next
    poll, watcher.sync_policy_changes detects the change against its
    last-recorded snapshot and fires a fresh dispatch under the new
    Policy — which re-flows the pipeline so beacon/ re-schedules the
    item's transmissions under the new tier. Only the transmit stage is
    affected: the item's data was already fetched and its content already
    processed. Returns whether a row was actually updated."""
    if policy is None:
        return False

    cursor = conn.execute(
        "UPDATE items SET policy = ? WHERE source = ? AND item_id = ?",
        (policy, source, item_id),
    )
    conn.commit()
    if cursor.rowcount > 0:
        record_audit_event(
            conn,
            event_type="item.policy_overridden",
            actor="dispatcher.override",
            source=source,
            item_id=item_id,
            details={"policy": policy},
        )
    return cursor.rowcount > 0


def rearm_item(conn: sqlite3.Connection, consumer: str, source: str, item_id: str) -> bool:
    """Re-enters an item at the **process** stage now for `consumer` — the
    "Reprocess" operation. Re-arms a fresh dispatch, so the `ai` and
    `chunk` actions run again (a fresh CloudEvent id, so they don't skip
    it) and re-publish `item.content_ready`, which re-schedules the item's
    transmission. Use it when the summary needs regenerating (prompt
    edited, bad output, item contents changed) or to simply put an item
    back on air under its current Policy. For an item that needs a
    *different* Policy, use override_item instead — watcher.
    sync_policy_changes picks that up on its own. Returns True if a row
    was inserted (False if the item doesn't exist in `items`, or is still
    armed — nothing to re-arm)."""
    _ensure_tables(conn)

    exists = conn.execute(
        "SELECT 1 FROM items WHERE source = ? AND item_id = ?", (source, item_id)
    ).fetchone()
    if exists is None:
        return False

    already_in_flight = conn.execute(
        "SELECT 1 FROM trigger_dispatches WHERE consumer = ? AND source = ? AND item_id = ?",
        (consumer, source, item_id),
    ).fetchone()
    if already_in_flight is not None:
        return False

    _arm(conn, consumer, source, item_id)
    conn.commit()
    record_audit_event(
        conn,
        event_type="item.reprocessed",
        actor="dispatcher.override",
        source=source,
        item_id=item_id,
        details={"consumer": consumer},
    )
    return True


def reset_dispatch_state(
    conn: sqlite3.Connection, consumer: str, source: str, item_id: str
) -> bool:
    """Debug/dev-tool operation (see ui/'s Developers section) — clears
    `consumer`'s trigger_dispatches and item_policy_state rows for this
    item without re-arming it, unlike rearm_item which inserts a due
    trigger_dispatches row. After this call the item is neither in-flight
    nor recorded as "seen" by this consumer at all: it stays untouched
    until its policy actually changes (caught by
    sync_policy_changes, which silently re-baselines a never-seen item
    rather than arming it) or it's explicitly rearmed. Meant for clearing
    stuck/incorrect dispatcher bookkeeping while debugging, not a normal
    delivery-control operation like override_item/rearm_item. Returns
    whether anything was actually cleared."""
    _ensure_tables(conn)

    trigger_cursor = conn.execute(
        "DELETE FROM trigger_dispatches WHERE consumer = ? AND source = ? AND item_id = ?",
        (consumer, source, item_id),
    )
    state_cursor = conn.execute(
        "DELETE FROM item_policy_state WHERE consumer = ? AND source = ? AND item_id = ?",
        (consumer, source, item_id),
    )
    conn.commit()

    cleared = trigger_cursor.rowcount > 0 or state_cursor.rowcount > 0
    if cleared:
        record_audit_event(
            conn,
            event_type="item.dispatch_state_reset",
            actor="ui.dev",
            source=source,
            item_id=item_id,
            details={"consumer": consumer},
        )
    return cleared
