"""The /about page: an informational, non-technical description of the
product. Visible to every logged-in user (admin and read-only 'user'),
redirects to /login when logged out, bilingual."""
import sqlite3

import pytest
from fastapi import Request
from fastapi.testclient import TestClient


@pytest.fixture
def logged_out_client(conn: sqlite3.Connection):
    """Only get_db overridden — get_current_user runs for real, so a
    request with no session cookie hits the NotAuthenticated -> /login
    redirect path (mirrors test_auth_routes.real_login_client)."""
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


def test_about_page_returns_200(client):
    response = client.get("/about")
    assert response.status_code == 200
    assert "About RadioBeacon" in response.text


def test_about_page_visible_to_read_only_user(user_client):
    # Proves the page is not behind the admin gate.
    assert user_client.get("/about").status_code == 200


def test_about_page_redirects_to_login_when_logged_out(logged_out_client):
    response = logged_out_client.get("/about", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_about_link_in_nav_for_admin_and_user(client, user_client):
    assert 'href="/about"' in client.get("/").text
    assert 'href="/about"' in user_client.get("/").text


def test_about_page_translated_to_spanish(client):
    response = client.get("/about", headers={"Accept-Language": "es"})
    assert '<html lang="es">' in response.text
    assert "Acerca de RadioBeacon" in response.text
    assert "baliza de propagación" in response.text


def test_about_page_shows_operator_credit(client):
    text = client.get("/about").text
    assert "Operator" in text
    assert "CD3DXZ" in text
    assert 'href="https://cd3dxz.radio"' in text
    assert 'target="_blank"' in text


def test_operator_credit_in_sitewide_footer(client, logged_out_client):
    # Footer on an authenticated page...
    footer = client.get("/").text
    assert 'class="app-footer"' in footer
    assert "Operated by" in footer
    assert "CD3DXZ" in footer
    # ...and on the login page.
    login = logged_out_client.get("/login").text
    assert "CD3DXZ" in login
    assert 'href="https://cd3dxz.radio"' in login


def test_operator_credit_translated_to_spanish(client):
    text = client.get("/", headers={"Accept-Language": "es"}).text
    assert "Operado por" in text


def test_about_sections_have_heading_icons(client):
    text = client.get("/about").text
    # Every about-section <h2> carries a decorative icon chip.
    assert text.count('<h2>') == text.count('<h2><svg class="icon"')
    assert text.count('<h2><svg class="icon"') >= 8


def test_about_page_has_pipeline_animation(client):
    text = client.get("/about").text
    assert 'class="about-pipeline"' in text
    assert 'class="pipe pipe--wide"' in text
    assert 'class="pipe pipe--tall"' in text
    assert "frequency is clear" in text  # caption
    # the built-in example sources are named, framed as examples
    assert "MeteoChile" in text
    assert "any public web feed" in text
    # the numbered fallback list is still present alongside it
    assert 'class="about-flow"' in text


def test_pipeline_caption_translated(client):
    text = client.get("/about", headers={"Accept-Language": "es"}).text
    assert "En cola" in text
    assert "la frecuencia está libre" in text


def test_about_page_has_under_the_hood(client):
    text = client.get("/about").text
    assert "Under the hood" in text
    assert "SvxLink" in text
    assert "Direwolf" in text


def test_under_the_hood_translated_to_spanish(client):
    text = client.get("/about", headers={"Accept-Language": "es"}).text
    assert "Por dentro" in text
    assert "SvxLink" in text  # product names stay untranslated


def test_about_page_has_flow_diagram(client):
    text = client.get("/about").text
    assert 'class="about-flow"' in text
    assert "Watch for updates" in text
    assert "On air" in text
    assert "recorded in this console" in text
