import dataclasses
import json
import logging
import sqlite3
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "storage" / "radiobeacon.db"

_audit_event_hooks: list[Callable[..., None]] = []


def register_audit_event_hook(hook: Callable[..., None]) -> None:
    """Registers a callback invoked (best-effort) after every successful
    record_audit_event() call, with the same keyword arguments
    record_audit_event() itself takes (event_type, actor, source, item_id,
    details). A hook that raises is logged and swallowed — never masks the
    caller's own operation, which has already committed by the time hooks
    run. Not called at import time by any package — callers register
    explicitly at startup, so importing this module never has network
    side effects on its own. Registering the same hook twice is a no-op."""
    if hook not in _audit_event_hooks:
        _audit_event_hooks.append(hook)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    extracted_title TEXT,
    extracted_contents TEXT,
    summary TEXT,
    url TEXT,
    event_key TEXT,
    type TEXT,
    subtype TEXT,
    dispatch_policy TEXT,
    source_date_time TEXT,
    fetched_at TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    rawdata TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);
"""

# audit_log is the one durable, queryable record of pipeline events —
# unlike stdlib logging (stdout only, not persisted). Every package writes
# to it exclusively through record_audit_event() below, never directly, so
# the column set/contract stays uniform regardless of which package or
# event produced a row. `source`/`item_id` are nullable: some events (e.g.
# a dispatch_policies edit) aren't about any one item.
_CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    source TEXT,
    item_id TEXT,
    details TEXT,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
_CREATE_AUDIT_LOG_SOURCE_ITEM_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_audit_log_source_item ON audit_log (source, item_id);"
)
_CREATE_AUDIT_LOG_EVENT_TYPE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log (event_type);"
)

# chunks is a durable, ordered record of every chunk a "chunk"-family
# action has produced — independent of whether anything was subscribed on
# MQTT to receive it. chunk_index/chunk_count preserve each chunk's
# position within its batch; UNIQUE(source, item_id, chunk_index) + the
# writer's INSERT OR IGNORE make storing the same batch twice a no-op
# (e.g. if a duplicate delivery somehow reaches run() despite the
# framework-level idempotency check in actions.__main__). No separate
# index needed beyond the UNIQUE constraint's own — it already covers
# (source, item_id) as a prefix, same as items' PRIMARY KEY does.
_CREATE_CHUNKS = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, item_id, chunk_index)
);
"""

# (old_column, new_column): renames applied in order to databases created
# before a given schema change.
_COLUMN_RENAMES = (
    ("data", "rawdata"),
    ("title", "extracted_title"),
    ("contents", "extracted_contents"),
    ("url_access", "event_key"),
)
_NEW_TEXT_COLUMNS = (
    "extracted_title",
    "extracted_contents",
    "summary",
    "url",
    "event_key",
    "type",
    "subtype",
    "dispatch_policy",
    "source_date_time",
)
# Columns from an older schema — dropped on migration if present: two
# summarized_* columns replaced by the single `summary` column, and
# urgency/repeat_times/repeat_interval_seconds replaced by the single
# dispatch_policy column (see dispatcher.policy for the centralized
# repeat/interval config it now points at).
_DROPPED_COLUMNS = (
    "summarized_title",
    "summarized_contents",
    "urgency",
    "repeat_times",
    "repeat_interval_seconds",
)


def _migrate_items_table(conn: sqlite3.Connection) -> None:
    """Patches an items table created before the current column set/names
    existed, so existing databases keep working as the schema evolves.
    Each adapter opens its own connection independently (see
    get_connection's docstring) — if two threads both open a connection
    against a still-unmigrated legacy DB at nearly the same moment, both
    can read the pre-migration column list before either commits, then
    both attempt the same ALTER TABLE. Whichever runs second gets
    sqlite3.OperationalError even though the migration step it wanted is
    already done (by the other connection) — caught per-statement below
    and treated as a no-op, not a failure, so this is self-healing
    without needing cross-connection locking."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if not columns:
        return  # table doesn't exist yet; SCHEMA above creates it fresh

    for old, new in _COLUMN_RENAMES:
        if old in columns and new not in columns:
            try:
                conn.execute(f"ALTER TABLE items RENAME COLUMN {old} TO {new}")
            except sqlite3.OperationalError:
                logger.debug(
                    "items.%s already renamed to %s by another connection", old, new
                )
            columns.discard(old)
            columns.add(new)

    for column in _NEW_TEXT_COLUMNS:
        if column not in columns:
            try:
                conn.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                logger.debug("items.%s already added by another connection", column)

    for column in _DROPPED_COLUMNS:
        if column in columns:
            try:
                conn.execute(f"ALTER TABLE items DROP COLUMN {column}")
            except sqlite3.OperationalError:
                logger.debug("items.%s already dropped by another connection", column)
            columns.discard(column)


def get_connection(
    db_path: str | Path = DEFAULT_DB_PATH, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Adapters now run as independent per-adapter loops on their own
    threads (see adapters.__main__), each opening its own connection to
    write on its own schedule — WAL mode lets those writers coexist with
    readers (e.g. query_history.sh) without blocking, and the longer
    busy_timeout (vs. Python's 5s default) gives a writer more room to
    wait out another adapter's write instead of raising "database is
    locked" on the rare occasion two fetches finish at nearly the same
    moment.

    check_same_thread=False is for a caller (e.g. ui.db.get_db) that opens
    one connection per logical unit of work but can't guarantee every
    step of that unit runs on the same OS thread — e.g. a framework whose
    threadpool executor may service a single request's dependency setup
    and its route handler body on two different worker threads. Safe
    there because the connection is still only ever touched sequentially
    within that one unit of work, never concurrently from two threads at
    once; sqlite3's same-thread check has no way to distinguish that from
    genuine concurrent cross-thread use, so it's disabled explicitly
    rather than worked around. Defaults to True (the check stays on),
    preserving every existing caller's behavior unchanged."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=check_same_thread)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate_items_table(conn)
        _ensure_audit_log_table(conn)
        _ensure_chunks_table(conn)
        conn.commit()
    except Exception:
        conn.close()
        raise
    logger.debug("opened sqlite connection at %s", path)
    return conn


def _ensure_audit_log_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection (not just ones from
    get_connection) — record_audit_event calls this itself, so a caller
    that hand-rolls a bare sqlite3 connection (e.g. a test) doesn't need to
    separately know about this table."""
    conn.execute(_CREATE_AUDIT_LOG)
    conn.execute(_CREATE_AUDIT_LOG_SOURCE_ITEM_INDEX)
    conn.execute(_CREATE_AUDIT_LOG_EVENT_TYPE_INDEX)


def record_audit_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    actor: str,
    source: str | None = None,
    item_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """The one contract every package writes audit rows through — same
    function, same column set, regardless of which package or which event
    produced it. `actor` identifies what wrote the row (e.g.
    "adapters.SenapredAdapter", "dispatcher.watcher"). `details` is optional free-form JSON (reuses
    _json_default for datetime/dataclass/Enum values, same as rawdata).
    After the row commits, every hook registered via
    register_audit_event_hook() is invoked with these same arguments —
    see that function for the contract (best-effort, never raises)."""
    _ensure_audit_log_table(conn)
    conn.execute(
        "INSERT INTO audit_log (event_type, actor, source, item_id, details) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            event_type,
            actor,
            source,
            item_id,
            json.dumps(details, default=_json_default) if details is not None else None,
        ),
    )
    conn.commit()

    for hook in _audit_event_hooks:
        try:
            hook(
                event_type=event_type,
                actor=actor,
                source=source,
                item_id=item_id,
                details=details,
            )
        except Exception:
            logger.error("audit event hook %r failed", hook, exc_info=True)


def _ensure_chunks_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — store_chunks calls
    this itself, same pattern as _ensure_audit_log_table."""
    conn.execute(_CREATE_CHUNKS)


def store_chunks(conn: sqlite3.Connection, chunks: list[dict[str, Any]]) -> int:
    """Persists chunks (the exact dicts actions.chunk.ChunkAction.run()
    builds) in order. Querying back with `ORDER BY chunk_index` always
    reconstructs the original sequence — insertion order alone isn't
    relied on for correctness, chunk_index is the authoritative position.
    Returns the number of newly stored rows (0 for a chunk_index already
    on file for that source/item_id — a duplicate batch, not an error)."""
    _ensure_chunks_table(conn)
    stored = 0
    for chunk in chunks:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO chunks (source, item_id, chunk_index, chunk_count, text) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                chunk["source"],
                chunk["item_id"],
                chunk["chunk_index"],
                chunk["chunk_count"],
                chunk["text"],
            ),
        )
        stored += cursor.rowcount
    conn.commit()
    return stored


def store_summary(conn: sqlite3.Connection, source: str, item_id: str, summary: str) -> bool:
    """Writes an AI-generated summary back onto an existing item (see
    actions.ai.AiAction). Mirrors dispatcher.override.override_item's
    shape: a plain UPDATE, returns whether a row was actually updated
    (False if source/item_id doesn't match any stored item)."""
    cursor = conn.execute(
        "UPDATE items SET summary = ? WHERE source = ? AND item_id = ?",
        (summary, source, item_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def _json_default(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def store_reading(conn: sqlite3.Connection, reading: Any) -> int:
    """Stores each item in reading.data, keyed by its `id`. Items without an
    `id` are skipped. Uniqueness (one row per source+id) is enforced by the
    items table's primary key.

    Items are treated as immutable once stored: an already-known id is
    left completely untouched — no column is refreshed, no write happens
    at all — only genuinely new items get inserted. This assumes an
    adapter never reuses an id for content that changes over time (true
    for SENAPRED: it publishes a new item, with a new id, for every update
    to an event rather than mutating an existing one — see `event_key`,
    which is what groups those separate immutable items into one event's
    history). If a future adapter's items legitimately do change under the
    same id, this function would need revisiting for that adapter.

    `title`, `contents`, `url`, `event_key`, `type`, `subtype`, and
    `source_date_time` are read from the item via duck typing (each
    optional) — this is the generic item contract adapters may implement,
    on top of the raw JSON serialization (`rawdata`) that's always stored
    regardless. `title`/`contents` land in
    `extracted_title`/`extracted_contents`.

    `event_key` (where an adapter has one) is the item's grouping/thread
    key — e.g. for SENAPRED, several items over time (declared, monitored,
    modified, cancelled) share the same `event_key` (SENAPRED's own
    `url_access`), forming one event's history. Persisting it lets that
    history be reconstructed later from already-stored rows:
    `WHERE event_key = ? ORDER BY source_date_time`.

    `type`/`subtype` are a generic two-level category — e.g. for SENAPRED,
    `type` is "Alerta"/"Evento" (which of its two separate GraphQL feeds
    an item came from) and `subtype` is the risk category (e.g.
    "Hidrometeorologico"). Any adapter with a similar broad/fine category
    split can use the same two columns.

    `dispatch_policy` (where an adapter has one) names which delivery
    policy dispatcher/ should use for this item — e.g. "urgent" or
    "informational". Meaningless to this package: it's a soft reference
    (not a SQL FOREIGN KEY) to the `name` column of dispatcher's
    `dispatch_policies` table, which centralizes the actual repeat
    count/interval config a name maps to (see
    dispatcher/src/dispatcher/policy.py). A row with no `dispatch_policy`
    (adapter doesn't implement it, value missing, or the name doesn't
    exist in `dispatch_policies`) falls back to dispatcher's default policy.

    `summary`, like `dispatch_policy`, is never touched here — adapters
    only propose an initial value (or leave it NULL/unset); a separate
    actor updates it afterward. `summary` starts NULL and stays that way
    until a separate actor sets it.
    `dispatch_policy` starts at whatever the adapter proposed and can be
    overridden afterward by a human/UI (via dispatcher/override_item.py) —
    unlike the rest of the row, this column is not meant to be
    immutable-forever, only adapter-untouched-after-insert.

    Returns the number of newly stored (previously unseen) items."""
    stored = 0
    skipped_no_id = 0
    failed = 0
    items = reading.data if isinstance(reading.data, list) else []
    for item in items:
        item_id = getattr(item, "id", None)
        if item_id is None:
            skipped_no_id += 1
            continue
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO items "
                "(source, item_id, extracted_title, extracted_contents, "
                "summary, url, event_key, type, subtype, dispatch_policy, "
                "source_date_time, fetched_at, rawdata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reading.source,
                    item_id,
                    _as_text(getattr(item, "title", None)),
                    _as_text(getattr(item, "contents", None)),
                    None,  # summary — never set here, populated later by a separate actor
                    _as_text(getattr(item, "url", None)),
                    _as_text(getattr(item, "event_key", None)),
                    _as_text(getattr(item, "type", None)),
                    _as_text(getattr(item, "subtype", None)),
                    _as_text(getattr(item, "dispatch_policy", None)),
                    _as_text(getattr(item, "source_date_time", None)),
                    reading.fetched_at.isoformat(),
                    json.dumps(item, default=_json_default),
                ),
            )
            if cursor.rowcount:
                record_audit_event(
                    conn,
                    event_type="item.stored",
                    actor="adapters.storage",
                    source=reading.source,
                    item_id=item_id,
                )
            stored += cursor.rowcount
        except Exception:
            # One malformed/unexpected item must never discard every item
            # processed earlier in this same batch — those are still
            # pending in this transaction and get committed below along
            # with everything else; only this one item is skipped.
            failed += 1
            logger.error(
                "source=%s: failed to store item_id=%s, skipping",
                reading.source,
                item_id,
                exc_info=True,
            )

    conn.commit()
    already_known = len(items) - stored - skipped_no_id - failed
    logger.info(
        "source=%s: %d new item(s) stored, %d already known (untouched), "
        "%d skipped (no id), %d failed",
        reading.source,
        stored,
        already_known,
        skipped_no_id,
        failed,
    )
    return stored
