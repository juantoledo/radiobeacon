"""Host / cross-origin / CSRF request gating — the dashboard has no auth, so
this layer is what stops another page in the operator's browser (or anything
else on the network) from driving it. See ui.security / ui.app."""
import sqlite3

import pytest
from fastapi import Request
from fastapi.testclient import TestClient


@pytest.fixture
def csrf_client(conn: sqlite3.Connection):
    """Like the shared `client` fixture but WITHOUT the verify_csrf override,
    so the real double-submit-cookie check runs."""
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


def _insert_item(conn):
    conn.execute(
        "INSERT INTO items (source, item_id, fetched_at, rawdata) "
        "VALUES ('csn', '1', datetime('now'), '{}')"
    )
    conn.commit()


def test_post_without_csrf_token_is_rejected(csrf_client, conn):
    _insert_item(conn)
    response = csrf_client.post("/items/csn/1/rearm", follow_redirects=False)
    assert response.status_code == 403
    assert "CSRF" in response.text


def test_post_with_matching_csrf_cookie_and_field_is_accepted(csrf_client, conn):
    _insert_item(conn)
    # A GET first, to be issued the csrf_token cookie.
    csrf_client.get("/items/csn/1")
    token = csrf_client.cookies["csrf_token"]

    response = csrf_client.post(
        "/items/csn/1/rearm",
        data={"consumer": "log", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_post_with_mismatched_csrf_token_is_rejected(csrf_client, conn):
    _insert_item(conn)
    csrf_client.get("/items/csn/1")
    response = csrf_client.post(
        "/items/csn/1/rearm",
        data={"consumer": "log", "csrf_token": "not-the-real-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_rendered_form_carries_the_csrf_hidden_field(csrf_client, conn):
    _insert_item(conn)
    html = csrf_client.get("/items/csn/1").text
    assert 'name="csrf_token"' in html


def test_post_from_a_foreign_origin_is_blocked(client, conn):
    conn.execute(
        "INSERT INTO items (source, item_id, fetched_at, rawdata) "
        "VALUES ('csn', '1', datetime('now'), '{}')"
    )
    conn.commit()

    response = client.post(
        "/items/csn/1/rearm",
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_post_from_an_allowed_origin_proceeds(client, conn):
    conn.execute(
        "INSERT INTO items (source, item_id, fetched_at, rawdata) "
        "VALUES ('csn', '1', datetime('now'), '{}')"
    )
    conn.commit()

    response = client.post(
        "/items/csn/1/rearm",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code != 403


def test_get_from_a_foreign_origin_is_allowed(client):
    # Safe method — nothing to protect, and blocking it would break embeds.
    response = client.get("/", headers={"origin": "http://evil.example"})
    assert response.status_code == 200


def test_unknown_host_header_is_rejected(client):
    response = client.get("/", headers={"host": "evil.example"})
    assert response.status_code == 400
