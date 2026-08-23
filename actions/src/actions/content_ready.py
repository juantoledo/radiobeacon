"""Watches for items where both actions.chunk and actions.ai have finished
reacting to the same dispatch, and publishes one item.content_ready
CloudEvent per item once they have — the single, race-free signal beacon
needs ("everything that was going to happen to this item's content has
happened") instead of guessing based on which of two independently-racing
actions happens to fire first.

Not an Action subclass (see base.py) — this correlates two independent
actions' completions rather than reacting to one message, so it's run from
its own poll loop in __main__.py instead of subscribing to a topic.

Polling, not the audit-event-hook mechanism (register_audit_event_hook in
adapters.storage): chunk and ai each run on their own independent MQTT
thread, so two hooks could observe "both done" at the same instant and
each publish once — a real double-publish race. A single poll loop,
touching the DB only within one tick at a time, serializes the check
instead.

action.<name>.executed audit rows are recorded on every normal run()
return — including skips (short content, ACTIONS_AI_ENABLED=false) — not
just when an action actually produced output; only an uncaught exception
skips the row (see __main__.py's _make_on_message). So "both rows exist"
is a reliable, non-heuristic "both actions are done deciding" signal
regardless of whether either produced anything.

Rearm doesn't delete prior audit_log rows, it produces new ones, so
"already published" is a timestamp comparison (item_readiness.published_at
vs. the latest of the two actions' recorded_at), not existence alone — a
rearm's fresh completions naturally produce settled_at > published_at,
triggering a fresh publish."""
import logging
import sqlite3
from typing import Any

from adapters.storage import (
    get_item_ready_published_at,
    mark_item_ready_published,
    record_audit_event,
)

from actions import mq

logger = logging.getLogger(__name__)

# Documented module constant, not a setting — this is about the
# content-preparation pipeline's fixed shape, not deployment tuning.
_REQUIRED_ACTIONS = ("chunk", "ai")
_EVENT_TYPES = tuple(f"action.{name}.executed" for name in _REQUIRED_ACTIONS)

OUTPUT_EVENT_TYPE = "item.content_ready"


def _find_settled_items(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Items with at least one action.<name>.executed row for every action
    in _REQUIRED_ACTIONS, along with the latest recorded_at across all of
    them ("settled_at") — the timestamp of whichever action most recently
    finished deciding what to do with this item."""
    placeholders = ",".join("?" for _ in _EVENT_TYPES)
    return conn.execute(
        f"""
        SELECT source, item_id, MAX(recorded_at) AS settled_at
        FROM audit_log
        WHERE event_type IN ({placeholders})
          AND source IS NOT NULL AND item_id IS NOT NULL
        GROUP BY source, item_id
        HAVING COUNT(DISTINCT event_type) = ?
        """,
        (*_EVENT_TYPES, len(_EVENT_TYPES)),
    ).fetchall()


def _has_summary(conn: sqlite3.Connection, source: str, item_id: str) -> bool:
    row = conn.execute(
        "SELECT summary FROM items WHERE source = ? AND item_id = ?", (source, item_id)
    ).fetchone()
    return bool(row and row[0])


def check_and_publish(
    conn: sqlite3.Connection,
    client: Any,
    *,
    output_topic: str,
    actor: str,
    qos: int,
) -> int:
    """One poll tick: find items newly settled since their last publish (or
    never published), publish one item.content_ready CloudEvent per item,
    and mark each published. Returns the count published (mainly for
    tests)."""
    published = 0
    for source, item_id, settled_at in _find_settled_items(conn):
        published_at = get_item_ready_published_at(conn, source, item_id)
        if published_at is not None and settled_at <= published_at:
            continue

        has_summary = _has_summary(conn, source, item_id)
        payload = mq.build_cloud_event_payload(
            event_type=OUTPUT_EVENT_TYPE,
            actor=actor,
            data={"source": source, "item_id": item_id, "has_summary": has_summary},
        )
        client.publish(output_topic, payload=payload, qos=qos)
        mark_item_ready_published(conn, source, item_id)
        record_audit_event(
            conn,
            event_type="item.content_ready.published",
            actor=actor,
            source=source,
            item_id=item_id,
            details={"has_summary": has_summary},
        )
        logger.info(
            "content_ready: source=%s item_id=%s published (has_summary=%s)",
            source, item_id, has_summary,
        )
        published += 1
    return published
