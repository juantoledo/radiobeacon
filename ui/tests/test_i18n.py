import sqlite3

from adapters.storage import set_setting
from fastapi.testclient import TestClient


def test_default_locale_no_accept_language_no_cookie(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert '<html lang="en">' in response.text
    assert "Dashboard" in response.text


def test_accept_language_es_selects_spanish(client: TestClient):
    response = client.get("/", headers={"Accept-Language": "es-CL,es;q=0.9,en;q=0.5"})
    assert response.status_code == 200
    assert '<html lang="es">' in response.text
    assert "Panel" in response.text


def test_accept_language_unsupported_falls_back_to_en(client: TestClient):
    response = client.get("/", headers={"Accept-Language": "fr-FR,fr;q=0.9"})
    assert response.status_code == 200
    assert '<html lang="en">' in response.text


def test_accept_language_unsupported_falls_back_to_ui_default_locale(
    client: TestClient, conn: sqlite3.Connection
):
    set_setting(conn, "UI_DEFAULT_LOCALE", "es")
    response = client.get("/", headers={"Accept-Language": "fr-FR,fr;q=0.9"})
    assert response.status_code == 200
    assert '<html lang="es">' in response.text


def test_locale_cookie_overrides_accept_language(client: TestClient):
    client.cookies.set("locale", "es")
    response = client.get("/", headers={"Accept-Language": "en"})
    assert response.status_code == 200
    assert '<html lang="es">' in response.text


def test_set_locale_endpoint_sets_cookie_and_redirects(client: TestClient):
    response = client.post(
        "/locale", data={"locale": "es", "next": "/items"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/items"
    assert response.cookies.get("locale") == "es"


def test_set_locale_endpoint_rejects_unsupported_locale(client: TestClient):
    response = client.post("/locale", data={"locale": "fr", "next": "/"}, follow_redirects=False)
    assert response.status_code == 400
    assert response.cookies.get("locale") is None


def test_set_locale_endpoint_ignores_external_next(client: TestClient):
    response = client.post(
        "/locale",
        data={"locale": "es", "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_ui_default_locale_setting_editable_at_config_ui(
    client: TestClient, conn: sqlite3.Connection
):
    response = client.get("/config/ui")
    assert response.status_code == 200

    save = client.post(
        "/config/ui",
        data={"UI_DEFAULT_LOCALE": "es"},
        follow_redirects=False,
    )
    assert save.status_code in (302, 303)

    response = client.get("/")
    assert '<html lang="es">' in response.text
