"""Write operations backing the Developers section (/dev) — direct
add/edit/delete on `items`. Deliberately separate from queries.py
(read-only) and from adapters.storage/dispatcher.* (which enforce the
"items are immutable after insert, except summary/transmit_policy"
contract — see adapters.storage.store_reading's docstring): the
Developers section is an explicit, clearly-labeled escape hatch around
that contract for debugging/backfilling, not a replacement for it. Every
write here gets its own audit_log event (actor="ui.dev") so a raw
edit/delete is never silently indistinguishable from a normal pipeline
write."""
import json
import sqlite3

from adapters.storage import record_audit_event
from adapters.timeutil import utc_now


def create_item(
    conn: sqlite3.Connection,
    *,
    source: str,
    item_id: str,
    extracted_title: str | None,
    extracted_contents: str | None,
    summary: str | None,
    url: str | None,
    event_key: str | None,
    type_: str | None,
    subtype: str | None,
    transmit_policy: str | None,
    source_date_time: str | None,
) -> None:
    """Raises sqlite3.IntegrityError if (source, item_id) already exists
    — the route calling this should catch it and show a friendly message
    rather than a raw 500. fetched_at and rawdata are always this
    function's own values, never caller-supplied: fetched_at is "now",
    and rawdata is a small marker object (there's no real adapter payload
    for a hand-created item) so a later reader can tell this apart from
    an adapter-fetched item just by looking at the column."""
    conn.execute(
        "INSERT INTO items "
        "(source, item_id, extracted_title, extracted_contents, summary, url, "
        "event_key, type, subtype, transmit_policy, source_date_time, "
        "fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            item_id,
            extracted_title,
            extracted_contents,
            summary,
            url,
            event_key,
            type_,
            subtype,
            transmit_policy,
            source_date_time,
            utc_now().isoformat(),
            json.dumps({"_created_via": "ui.dev"}),
        ),
    )
    conn.commit()
    record_audit_event(
        conn,
        event_type="item.created",
        actor="ui.dev",
        source=source,
        item_id=item_id,
        details={"transmit_policy": transmit_policy},
    )


def update_item(
    conn: sqlite3.Connection,
    source: str,
    item_id: str,
    *,
    extracted_title: str | None,
    extracted_contents: str | None,
    summary: str | None,
    url: str | None,
    event_key: str | None,
    type_: str | None,
    subtype: str | None,
    transmit_policy: str | None,
    source_date_time: str | None,
) -> bool:
    """Full-row edit of every field except the primary key (source,
    item_id) and the write-once bookkeeping columns (fetched_at,
    captured_at, rawdata). The primary key is fixed on purpose: changing
    it would silently orphan this item's existing chunks/audit_log/
    trigger_dispatches/item_policy_state rows, which all reference the
    *current* (source, item_id) — to "rename" an item, delete and
    recreate it instead. Returns whether a row was actually updated."""
    cursor = conn.execute(
        "UPDATE items SET extracted_title = ?, extracted_contents = ?, summary = ?, "
        "url = ?, event_key = ?, type = ?, subtype = ?, transmit_policy = ?, "
        "source_date_time = ? WHERE source = ? AND item_id = ?",
        (
            extracted_title,
            extracted_contents,
            summary,
            url,
            event_key,
            type_,
            subtype,
            transmit_policy,
            source_date_time,
            source,
            item_id,
        ),
    )
    conn.commit()
    if cursor.rowcount > 0:
        record_audit_event(
            conn,
            event_type="item.dev_edited",
            actor="ui.dev",
            source=source,
            item_id=item_id,
            details={"transmit_policy": transmit_policy},
        )
    return cursor.rowcount > 0


def delete_item(conn: sqlite3.Connection, source: str, item_id: str) -> bool:
    """Deletes an item and its operational rows — chunks,
    trigger_dispatches/item_policy_state, and beacon_tx_schedule (all
    scoped to this item, and meaningless once it's gone). audit_log rows
    are deliberately kept as a historical record of what happened while
    the item existed, and this deletion itself is recorded as one more
    audit_log row — even though the items row it references is now gone,
    audit_log's source/item_id are plain text columns, not a SQL FOREIGN
    KEY, so a historical reference to a since-deleted item is fine.
    Returns whether an items row was actually deleted."""
    conn.execute("DELETE FROM chunks WHERE source = ? AND item_id = ?", (source, item_id))
    conn.execute(
        "DELETE FROM trigger_dispatches WHERE source = ? AND item_id = ?", (source, item_id)
    )
    conn.execute(
        "DELETE FROM item_policy_state WHERE source = ? AND item_id = ?", (source, item_id)
    )
    conn.execute(
        "DELETE FROM beacon_tx_schedule WHERE source = ? AND item_id = ?", (source, item_id)
    )
    cursor = conn.execute("DELETE FROM items WHERE source = ? AND item_id = ?", (source, item_id))
    conn.commit()

    if cursor.rowcount > 0:
        record_audit_event(
            conn, event_type="item.deleted", actor="ui.dev", source=source, item_id=item_id
        )
    return cursor.rowcount > 0
