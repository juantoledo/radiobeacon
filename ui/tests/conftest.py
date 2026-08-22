import sqlite3
import tempfile
from pathlib import Path

import adapters.storage as storage_module
import pytest
from adapters.storage import get_connection
from dispatcher.watcher import _ensure_tables
from fastapi.testclient import TestClient

# get_setting() (used by the /config routes' get_setting(spec.key, ...,
# conn=conn) calls, and by anything else that omits conn/db_path) falls
# back to opening its own connection against DEFAULT_DB_PATH. Every route
# test here already overrides ui.db.get_db to use the `conn` fixture below,
# but this redirect is a safety net for any code path that doesn't go
# through that override — see the equivalent conftest.py in the other
# three packages' test suites.
storage_module.DEFAULT_DB_PATH = Path(tempfile.mkdtemp(prefix="radiobeacon-test-db-")) / (
    "radiobeacon.db"
)
# Actually create the file (fully migrated schema, dispatcher tables
# included) — ui.db.open_readonly_connection() (the /dev/sql runner) opens
# it in SQLite's read-only URI mode, which (unlike a normal connect) can't
# create a missing file.
_default_db_conn = storage_module.get_connection(storage_module.DEFAULT_DB_PATH)
_ensure_tables(_default_db_conn)
_default_db_conn.close()


@pytest.fixture
def conn():
    """A real, fully-migrated in-memory schema — adapters.storage.get_connection
    plus dispatcher.watcher._ensure_tables, not a stripped-down hand-rolled
    CREATE TABLE — so queries.py's SQL is checked against the actual
    production schema rather than a stand-in that could drift from it."""
    # check_same_thread=False: the `client` fixture below hands this same
    # connection to a running app, whose threadpool executor may touch it
    # from a different OS thread than this fixture's own — see
    # adapters.storage.get_connection's docstring.
    connection = get_connection(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    _ensure_tables(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(conn: sqlite3.Connection):
    """A TestClient wired to the same in-memory connection every route
    test's assertions inspect afterward — overrides ui.db.get_db rather
    than pointing UI_DB_PATH at a real file, so a test can both drive the
    app through HTTP and query `conn` directly to check the resulting
    row/audit_log state."""
    from ui.app import app
    from ui.db import get_db

    def _override():
        yield conn

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
