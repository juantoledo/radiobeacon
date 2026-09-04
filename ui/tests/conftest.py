import os
import sqlite3
import tempfile
from pathlib import Path

# The shipped default is UI_ALLOWED_HOSTS="*" (the host/cross-origin guard
# off, matching the all-interfaces UI_HOST default). Pin a restrictive
# allow-list before ui.config is imported so test_security's DNS-rebinding /
# foreign-origin cases still exercise the guard.
os.environ.setdefault("UI_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver,[::1]")

import adapters.storage as storage_module
import pytest
from adapters.storage import get_connection
from dispatcher.watcher import _ensure_tables
from fastapi import Request
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
    from ui.security import verify_csrf

    # CSRF enforcement is exercised directly in test_security.py; every other
    # route test drives the app without juggling a token, the same way
    # Django's test client disables CSRF by default.
    app.dependency_overrides[verify_csrf] = lambda: None

    def _override(request: Request):
        # Mirrors the real get_db()'s request.state.db_conn stash — see
        # its docstring — so ui.templating's is_beacon_configured Jinja
        # global reuses this exact in-memory `conn` instead of opening a
        # second connection to DEFAULT_DB_PATH, which would never see
        # what a test wrote to `conn`.
        request.state.db_conn = conn
        yield conn

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(verify_csrf, None)
