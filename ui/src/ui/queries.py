"""Read-only SELECT helpers backing the UI's pages. New here, not bolted
onto adapters.storage or dispatcher.* — none of these queries exist
anywhere yet (those packages expose only writes plus the narrow reads
their own logic needs), and the UI is architecturally a third, independent
consumer of storage/radiobeacon.db, same as data-adapters/dispatcher are
already deliberately decoupled from each other. Every function here takes
an already-open sqlite3.Connection (row_factory = sqlite3.Row, set by
db.get_db) and does exactly one parameterized query (or two, for the
paginated list_* helpers: a COUNT(*) plus the paged SELECT — the same
simple two-query style already used elsewhere in this repo, no window
functions)."""
import sqlite3

ItemsPage = tuple[list[sqlite3.Row], int]
AuditPage = tuple[list[sqlite3.Row], int]


def list_items(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    type_: str | None = None,
    policy: str | None = None,
    event_key: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ItemsPage:
    where = []
    params: list[object] = []

    if source:
        where.append("source = ?")
        params.append(source)
    if type_:
        where.append("type = ?")
        params.append(type_)
    if policy:
        where.append("policy = ?")
        params.append(policy)
    if event_key:
        where.append("event_key = ?")
        params.append(event_key)
    if q:
        where.append("(extracted_title LIKE ? OR extracted_contents LIKE ? OR url LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])

    clause = f" WHERE {' AND '.join(where)}" if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM items{clause}", params
    ).fetchone()[0]

    # source_date_time (the event's own time, per the adapter/source) is
    # what a person cares about here — captured_at is just this repo's
    # own ingestion bookkeeping, not a meaningful default sort key.
    # rowid DESC is a secondary tiebreaker only, for a stable order among
    # rows sharing a source_date_time (including the common case of it
    # being NULL for every row, e.g. an adapter that doesn't set it).
    rows = conn.execute(
        f"SELECT * FROM items{clause} "
        "ORDER BY source_date_time DESC, rowid DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    return rows, total


def get_item(conn: sqlite3.Connection, source: str, item_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM items WHERE source = ? AND item_id = ?", (source, item_id)
    ).fetchone()


def list_item_chunks(conn: sqlite3.Connection, source: str, item_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT chunk_index, chunk_count, text FROM chunks "
        "WHERE source = ? AND item_id = ? ORDER BY chunk_index",
        (source, item_id),
    ).fetchall()


def list_audit_log_for_item(
    conn: sqlite3.Connection, source: str, item_id: str, limit: int = 200
) -> list[sqlite3.Row]:
    # Uses idx_audit_log_source_item (see adapters.storage.SCHEMA).
    return conn.execute(
        "SELECT * FROM audit_log WHERE source = ? AND item_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (source, item_id, limit),
    ).fetchall()


_FAILURE_EVENT_TYPE_LIKE = "%failed%"

_SINCE_WINDOWS = {"24h": "-1 day", "7d": "-7 day", "30d": "-30 day"}


def list_audit_log(
    conn: sqlite3.Connection,
    *,
    event_type: str | None = None,
    source: str | None = None,
    item_id: str | None = None,
    q: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AuditPage:
    where = []
    params: list[object] = []

    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    if source:
        where.append("source = ?")
        params.append(source)
    if item_id:
        where.append("item_id = ?")
        params.append(item_id)
    if q:
        where.append("(event_type LIKE ? OR actor LIKE ? OR details LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle, needle, needle])
    if status == "failed":
        where.append("event_type LIKE ?")
        params.append(_FAILURE_EVENT_TYPE_LIKE)
    if since in _SINCE_WINDOWS:
        where.append("recorded_at >= datetime('now', ?)")
        params.append(_SINCE_WINDOWS[since])

    clause = f" WHERE {' AND '.join(where)}" if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM audit_log{clause}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"SELECT * FROM audit_log{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    return rows, total


def dashboard_counts(conn: sqlite3.Connection) -> dict[str, int]:
    # The 24h cutoff is computed in SQL (datetime('now', '-1 day')),
    # deliberately, rather than parsing captured_at in Python — that
    # column is written via SQLite's own datetime('now') and lacks the
    # offset marker Python's isoformat() adds (see root README's "Dates
    # and times: always UTC" note); comparing two SQLite-native strings
    # here sidesteps that format mismatch entirely.
    return {
        "total_items": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "items_last_24h": conn.execute(
            "SELECT COUNT(*) FROM items WHERE captured_at >= datetime('now', '-1 day')"
        ).fetchone()[0],
        "total_audit_events": conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
        "events_last_24h": conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE recorded_at >= datetime('now', '-1 day')"
        ).fetchone()[0],
        "in_flight_dispatches": conn.execute(
            "SELECT COUNT(*) FROM trigger_dispatches"
        ).fetchone()[0],
        "total_policies": conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0],
    }


def items_sparkline(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    """Item ingest count per calendar day (UTC) for the last `days` days,
    oldest first, gap-filled with zeros — feeds the dashboard's inline
    sparkline. Buckets on captured_at (this repo's ingestion clock), which
    is SQLite-native `datetime('now')` text, so the whole bucket/compare
    stays in SQL for the same format-mismatch reason dashboard_counts does."""
    rows = conn.execute(
        "SELECT date(captured_at) AS d, COUNT(*) AS n FROM items "
        "WHERE captured_at >= datetime('now', ?) GROUP BY d",
        (f"-{days - 1} days",),
    ).fetchall()
    by_day = {r["d"]: r["n"] for r in rows}
    today = conn.execute("SELECT date('now')").fetchone()[0]
    start = conn.execute("SELECT date('now', ?)", (f"-{days - 1} days",)).fetchone()[0]
    out: list[dict] = []
    cur = start
    while cur <= today:
        out.append({"day": cur, "count": by_day.get(cur, 0)})
        cur = conn.execute("SELECT date(?, '+1 day')", (cur,)).fetchone()[0]
    return out


def recent_items(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    # Same default ordering as list_items — see its comment.
    return conn.execute(
        "SELECT * FROM items ORDER BY source_date_time DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()


def recent_audit_events(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def recent_watermark_transmits(conn: sqlite3.Connection, limit: int = 8) -> list[sqlite3.Row]:
    """Most recent successful watermark broadcasts, newest first -- for the
    dashboard's Items feed, which has no `items` row to represent them (a
    watermark carries no source/item_id) but should still show that one
    went out."""
    return conn.execute(
        "SELECT * FROM audit_log WHERE event_type = 'beacon.watermark.transmitted' "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def recent_manual_transmits(conn: sqlite3.Connection, limit: int = 8) -> list[sqlite3.Row]:
    """Most recent successful manual "Transmit now" sends, newest first --
    for the dashboard's Items feed, which has no `items` row to represent
    them (a manual send carries no source/item_id) but should still show
    that one went out, with playback when its clip is still on disk."""
    return conn.execute(
        "SELECT * FROM audit_log WHERE event_type = 'beacon.manual.transmitted' "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def latest_event_at(conn: sqlite3.Connection, *event_types: str) -> str | None:
    """recorded_at of the most recent audit_log row matching any of
    event_types (or any row at all if none given) — a cheap "when did X
    last happen" probe for the dashboard's status tiles."""
    if event_types:
        placeholders = ",".join("?" for _ in event_types)
        row = conn.execute(
            f"SELECT recorded_at FROM audit_log WHERE event_type IN ({placeholders}) "
            "ORDER BY id DESC LIMIT 1",
            event_types,
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT recorded_at FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row[0] if row else None


def failed_events_last_24h(conn: sqlite3.Connection, *, since_id: int = 0) -> int:
    """Count of audit rows in the last 24h for a failure event — the repo
    writes both `*.transmit_failed`/`*.dispatch_failed` and the dot form
    `action.ai.failed`, so match `failed` anywhere in the type. Surfaced
    on the dashboard as a pipeline-health warning banner.

    since_id (default 0, i.e. no floor) excludes rows the admin has
    already acknowledged by following the banner's own link — see
    ui.audit_ack. audit_log.id is AUTOINCREMENT, so it orders the same as
    recorded_at without the ambiguity of comparing timestamp strings."""
    return conn.execute(
        "SELECT COUNT(*) FROM audit_log "
        "WHERE recorded_at >= datetime('now', '-1 day') AND event_type LIKE ? AND id > ?",
        (_FAILURE_EVENT_TYPE_LIKE, since_id),
    ).fetchone()[0]


def max_audit_log_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]


def distinct_audit_event_types(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT event_type FROM audit_log ORDER BY event_type"
        )
    ]


def distinct_audit_sources(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT source FROM audit_log WHERE source IS NOT NULL ORDER BY source"
        )
    ]


def distinct_sources(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute("SELECT DISTINCT source FROM items ORDER BY source")
    ]


def distinct_types(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT type FROM items WHERE type IS NOT NULL ORDER BY type"
        )
    ]


def last_adapter_fetch_events(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """The most recent "adapter.fetch" audit_log row per source — a quick
    health signal for the /config/adapters list page (ok/error, when it last ran)
    without needing a dedicated status table. audit_log.id is
    AUTOINCREMENT, so MAX(id) per source is also the most recent row."""
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE id IN ("
        "  SELECT MAX(id) FROM audit_log WHERE event_type = 'adapter.fetch' GROUP BY source"
        ")"
    ).fetchall()
    return {row["source"]: row for row in rows}


def get_policy_row(conn: sqlite3.Connection, name: str):
    """The full `policies` row (a PolicyRow with every stage's fields +
    description) for the edit form to prefill. Delegates to
    adapters.policy.get_policy — kept here as the ui's stable entry point."""
    from adapters.policy import get_policy

    return get_policy(conn, name)
