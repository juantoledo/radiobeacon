import sqlite3

from adapters.storage import set_setting
from fastapi.testclient import TestClient

from ui.ajax import AJAX_HEADER, AJAX_HEADER_VALUE


def test_default_theme_no_cookie_is_system_no_data_theme_attr(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "data-theme=" not in response.text


def test_theme_cookie_light_sets_data_theme_attr(client: TestClient):
    client.cookies.set("theme", "light")
    response = client.get("/")
    assert response.status_code == 200
    assert 'data-theme="light"' in response.text


def test_theme_cookie_dark_sets_data_theme_attr(client: TestClient):
    client.cookies.set("theme", "dark")
    response = client.get("/")
    assert response.status_code == 200
    assert 'data-theme="dark"' in response.text


def test_theme_cookie_invalid_value_falls_back_to_system(client: TestClient):
    client.cookies.set("theme", "solarized")
    response = client.get("/")
    assert response.status_code == 200
    assert "data-theme=" not in response.text


def test_set_theme_endpoint_sets_cookie_and_redirects(client: TestClient):
    response = client.post(
        "/theme", data={"theme": "dark", "next": "/items"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/items"
    assert response.cookies.get("theme") == "dark"


def test_set_theme_endpoint_rejects_unsupported_theme(client: TestClient):
    response = client.post("/theme", data={"theme": "solarized", "next": "/"}, follow_redirects=False)
    assert response.status_code == 400
    assert response.cookies.get("theme") is None


def test_set_theme_endpoint_ajax_sets_cookie_and_returns_ok_json(client: TestClient):
    response = client.post(
        "/theme",
        data={"theme": "dark", "next": "/items"},
        headers={AJAX_HEADER: AJAX_HEADER_VALUE},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": ""}
    assert response.cookies.get("theme") == "dark"


def test_set_theme_endpoint_ajax_rejects_unsupported_theme(client: TestClient):
    response = client.post(
        "/theme",
        data={"theme": "solarized", "next": "/"},
        headers={AJAX_HEADER: AJAX_HEADER_VALUE},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert response.cookies.get("theme") is None


def test_set_theme_endpoint_ignores_external_next(client: TestClient):
    response = client.post(
        "/theme",
        data={"theme": "dark", "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_ui_default_theme_setting_editable_at_config_ui(
    client: TestClient, conn: sqlite3.Connection
):
    response = client.get("/config/ui")
    assert response.status_code == 200

    save = client.post(
        "/config/ui",
        data={"UI_DEFAULT_THEME": "dark"},
        follow_redirects=False,
    )
    assert save.status_code in (302, 303)

    response = client.get("/")
    assert 'data-theme="dark"' in response.text


def test_theme_cookie_overrides_ui_default_theme_setting(
    client: TestClient, conn: sqlite3.Connection
):
    set_setting(conn, "UI_DEFAULT_THEME", "dark")
    client.cookies.set("theme", "light")
    response = client.get("/")
    assert response.status_code == 200
    assert 'data-theme="light"' in response.text
