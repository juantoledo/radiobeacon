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
    transmit_policy: str | None = None,
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
    if transmit_policy:
        where.append("transmit_policy = ?")
        params.append(transmit_policy)
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


def list_audit_log(
    conn: sqlite3.Connection,
    *,
    event_type: str | None = None,
    source: str | None = None,
    item_id: str | None = None,
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
        "in_flight_dispatches": conn.execute(
            "SELECT COUNT(*) FROM trigger_dispatches"
        ).fetchone()[0],
        "total_policies": conn.execute("SELECT COUNT(*) FROM transmit_policies").fetchone()[0],
    }


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


def get_policy_row(
    conn: sqlite3.Connection, name: str
) -> tuple[str, int, int, str | None] | None:
    """Full transmit_policies row including `description` — unlike
    adapters.transmit_policy.get_policy(), which deliberately returns only
    the RepeatPolicy (repeat_times, interval_seconds) beacon logic needs.
    The edit form needs description to prefill, so it lives here."""
    row = conn.execute(
        "SELECT name, repeat_times, interval_seconds, description "
        "FROM transmit_policies WHERE name = ?",
        (name,),
    ).fetchone()
    return tuple(row) if row is not None else None
