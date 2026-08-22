"""FastAPI dependency handing each request its own sqlite3.Connection.

One connection per request, not a shared one: FastAPI runs sync `def`
routes in a threadpool, and sqlite3.Connection isn't safe to share across
threads without external locking (which would serialize every request and
defeat WAL). This is "each consumer opens its own connection" — the same
pattern every other package in this repo already follows — just at
request granularity instead of process granularity."""
from collections.abc import Iterator
from pathlib import Path
import sqlite3

from adapters.storage import DEFAULT_DB_PATH, get_connection
from dispatcher.watcher import _ensure_tables
from fastapi import Request

from . import config


def open_readonly_connection() -> sqlite3.Connection:
    """Opens storage/radiobeacon.db via SQLite's own read-only URI mode
    (mode=ro) — the Developers SQL runner's actual safety boundary.
    ui.sql_guard's query validation is defense in depth, not the
    boundary itself: even if that validation is ever wrong or bypassed,
    SQLite refuses any write against a mode=ro connection at the driver
    level, before the query text's meaning matters at all.

    Not a FastAPI dependency like get_db — the caller needs to catch a
    failed open (e.g. no database file yet) and render a normal error
    message, which Depends()'s generator-teardown machinery makes
    awkward; the caller is expected to close() this in a finally."""
    db_path = Path(config.UI_DB_PATH or DEFAULT_DB_PATH).resolve()
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    # check_same_thread=False: FastAPI's threadpool executor may run this
    # generator's setup and the route handler body on two different OS
    # threads for the same request — see get_connection's docstring.
    conn = get_connection(config.UI_DB_PATH or DEFAULT_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # dispatch_policies/trigger_dispatches/item_policy_state aren't created
    # by get_connection() alone, only by dispatcher.watcher._ensure_tables
    # — called unconditionally here, same as override_item.py/policies.py
    # already do regardless of which action is actually requested.
    _ensure_tables(conn)
    # Stashed so templating.py's is_beacon_configured Jinja global can
    # reuse this exact connection instead of opening a second one to
    # DEFAULT_DB_PATH — critical in tests, where the `client` fixture
    # overrides this dependency to yield an isolated in-memory connection
    # that a second, independently-opened connection would never see.
    request.state.db_conn = conn
    try:
        yield conn
    finally:
        conn.close()
