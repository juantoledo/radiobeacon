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


def _create_test_user(conn: sqlite3.Connection, username: str, role: str):
    """create_user() writes a user.created audit_log row like any other
    write path — cleared right back out here so a test using `client`/
    `user_client` still sees the empty audit_log its pre-auth version did;
    several existing tests (e.g. test_audit_log_returns_200) assert on
    that emptiness directly."""
    from adapters.auth import create_user

    user = create_user(conn, username, "test-password", role, actor="test")
    conn.execute("DELETE FROM audit_log")
    conn.commit()
    return user


@pytest.fixture
def admin_user(conn: sqlite3.Connection):
    return _create_test_user(conn, "admin", "admin")


@pytest.fixture
def plain_user(conn: sqlite3.Connection):
    return _create_test_user(conn, "operator", "user")


def _make_client(conn: sqlite3.Connection, user, *, seed_setup_complete: bool = True):
    """Shared machinery behind `client`/`user_client` below: overrides
    ui.db.get_db the same way regardless of which user is logged in, and
    overrides ui.current_user.get_current_user directly (rather than
    round-tripping through a real session cookie) — simplest way to log
    a TestClient in as a given role, consistent with this fixture already
    overriding get_db/verify_csrf via app.dependency_overrides instead of
    exercising the real HTTP mechanics. test_auth_routes.py's login/logout
    tests exercise the real cookie/session path directly, unaffected by
    this override since they build their own TestClient.

    seed_setup_complete=True (the default) marks the /setup wizard done on
    this `conn` before the client is handed back — every existing route
    test predates the wizard and assumes unblocked access to the dashboard/
    beacon/items routers, which ui.setup.require_setup_complete would
    otherwise redirect an admin away from on a fresh, unmigrated-by-a-wizard
    `conn`. Setup-wizard tests themselves want the un-configured state, so
    they use `fresh_install_client` (seed_setup_complete=False) instead."""
    from ui.app import app
    from ui.current_user import get_current_user
    from ui.db import get_db
    from ui.security import verify_csrf
    from ui.setup import mark_setup_complete

    if seed_setup_complete:
        # set_setting (which this calls) writes its own setting.changed
        # audit_log row — cleared right back out, same idiom as
        # _create_test_user's post-creation cleanup above, so this seeding
        # stays invisible to tests asserting an empty audit_log.
        mark_setup_complete(conn, actor="test")
        conn.execute("DELETE FROM audit_log")
        conn.commit()

    # CSRF enforcement is exercised directly in test_security.py; every other
    # route test drives the app without juggling a token, the same way
    # Django's test client disables CSRF by default.
    app.dependency_overrides[verify_csrf] = lambda: None

    def _db_override(request: Request):
        # Mirrors the real get_db()'s request.state.db_conn stash — see
        # its docstring — so ui.templating's is_beacon_configured Jinja
        # global reuses this exact in-memory `conn` instead of opening a
        # second connection to DEFAULT_DB_PATH, which would never see
        # what a test wrote to `conn`.
        request.state.db_conn = conn
        yield conn

    def _user_override(request: Request):
        request.state.user = user
        return user

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_current_user] = _user_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(verify_csrf, None)
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def client(conn: sqlite3.Connection, admin_user):
    """A TestClient wired to the same in-memory connection every route
    test's assertions inspect afterward, logged in as `admin_user` by
    default — every existing route test already assumed unauthenticated-
    but-unblocked access, which after adding login is equivalent to
    admin-authenticated access, so this is what keeps them passing without
    touching their bodies. See `user_client` for the 'user'-role
    equivalent, used by tests that specifically assert the role gate."""
    yield from _make_client(conn, admin_user)


@pytest.fixture
def user_client(conn: sqlite3.Connection, plain_user):
    """Same wiring as `client`, but logged in as the 'user' role — for
    tests asserting that role is actually blocked from admin-only routes."""
    yield from _make_client(conn, plain_user)


@pytest.fixture
def fresh_install_client(conn: sqlite3.Connection, admin_user):
    """Same wiring as `client` (admin-logged-in), but WITHOUT marking the
    /setup wizard complete — for tests exercising the fresh-install
    "not set up yet" state itself (test_setup_routes.py)."""
    yield from _make_client(conn, admin_user, seed_setup_complete=False)


@pytest.fixture
def fresh_install_user_client(conn: sqlite3.Connection, plain_user):
    """Same wiring as `user_client` ('user'-role-logged-in), but WITHOUT
    marking the /setup wizard complete — proves ui.setup.require_setup_complete
    never redirects a non-admin even on a fresh, unconfigured install."""
    yield from _make_client(conn, plain_user, seed_setup_complete=False)
