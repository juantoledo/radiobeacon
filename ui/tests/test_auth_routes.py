"""Login/logout exercised through the real cookie/session mechanics (not the
get_current_user override the shared `client` fixture uses) — this is the
one place that override would defeat the point of the test."""
import sqlite3

import pytest
from adapters.auth import create_user, validate_session
from fastapi import Request
from fastapi.testclient import TestClient


@pytest.fixture
def real_login_client(conn: sqlite3.Connection):
    """A TestClient with only get_db overridden — verify_csrf and
    get_current_user run for real, so a login POST needs a real CSRF
    token and actually walks the create_session/set_cookie path."""
    from ui.app import app
    from ui.db import get_db

    def _override(request: Request):
        request.state.db_conn = conn
        yield conn

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _csrf_token(client: TestClient, path: str) -> str:
    client.get(path)
    return client.cookies["csrf_token"]


def test_login_page_renders_when_logged_out(real_login_client):
    response = real_login_client.get("/login")
    assert response.status_code == 200
    assert 'name="username"' in response.text


def test_dashboard_redirects_to_login_when_logged_out(real_login_client):
    response = real_login_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_successful_login_sets_cookie_and_redirects(real_login_client, conn):
    create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = _csrf_token(real_login_client, "/login")

    response = real_login_client.post(
        "/login",
        data={"username": "alice", "password": "hunter2", "next": "/", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "session_token" in response.cookies

    # The session cookie actually works for a subsequent request.
    dashboard = real_login_client.get("/")
    assert dashboard.status_code == 200


def test_wrong_password_shows_generic_error_and_no_cookie(real_login_client, conn):
    create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = _csrf_token(real_login_client, "/login")

    response = real_login_client.post(
        "/login",
        data={"username": "alice", "password": "wrong", "next": "/", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Invalid username or password" in response.text
    assert "session_token" not in response.cookies


def test_unknown_username_shows_the_same_generic_error(real_login_client, conn):
    token = _csrf_token(real_login_client, "/login")
    response = real_login_client.post(
        "/login",
        data={"username": "ghost", "password": "whatever", "next": "/", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Invalid username or password" in response.text


def test_next_redirect_rejects_absolute_and_protocol_relative_urls(real_login_client, conn):
    create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = _csrf_token(real_login_client, "/login")

    for bad_next in ("https://evil.example", "//evil.example"):
        response = real_login_client.post(
            "/login",
            data={"username": "alice", "password": "hunter2", "next": bad_next, "csrf_token": token},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/"
        # Log out again so the next iteration re-exercises a fresh login.
        real_login_client.cookies.delete("session_token")


def test_logout_clears_cookie_and_invalidates_session(real_login_client, conn):
    create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = _csrf_token(real_login_client, "/login")
    real_login_client.post(
        "/login",
        data={"username": "alice", "password": "hunter2", "next": "/", "csrf_token": token},
        follow_redirects=False,
    )
    session_token = real_login_client.cookies["session_token"]
    assert validate_session(conn, session_token) is not None

    csrf_token = real_login_client.cookies["csrf_token"]
    response = real_login_client.post(
        "/logout", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert validate_session(conn, session_token) is None
