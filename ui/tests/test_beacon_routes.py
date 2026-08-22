from adapters.storage import get_setting, set_setting

from ui.beacon import BEACON_FIELDS, is_beacon_configured

ALL_VALUES = {
    "BEACON_CALLSIGN": "CD3DXZ-1",
    "BEACON_DESCRIPTION": "Experimental VHF propagation beacon",
    "BEACON_SHORT_DESCRIPTION": "CD3DXZ-1 propagation beacon",
    "BEACON_OPERATOR_CONTACT": "operator@example.com",
    "BEACON_GRID_LOCATOR": "FF46vb",
    "BEACON_FREQUENCY": "144.390 MHz",
}


def _configure_beacon(conn):
    for key, value in ALL_VALUES.items():
        set_setting(conn, key, value)


# --- is_beacon_configured (direct unit tests) ---


def test_is_beacon_configured_false_when_nothing_set(conn):
    assert is_beacon_configured(conn) is False


def test_is_beacon_configured_false_when_partially_set(conn):
    set_setting(conn, "BEACON_CALLSIGN", "CD3DXZ-1")

    assert is_beacon_configured(conn) is False


def test_is_beacon_configured_true_when_all_fields_set(conn):
    _configure_beacon(conn)

    assert is_beacon_configured(conn) is True


def test_is_beacon_configured_ignores_blank_value(conn):
    _configure_beacon(conn)
    set_setting(conn, "BEACON_CALLSIGN", "")

    assert is_beacon_configured(conn) is False


def test_is_beacon_configured_ignores_whitespace_only_value(conn):
    _configure_beacon(conn)
    set_setting(conn, "BEACON_CALLSIGN", "   ")

    assert is_beacon_configured(conn) is False


# --- GET /beacon ---


def test_beacon_setup_page_returns_200(client):
    response = client.get("/beacon")

    assert response.status_code == 200
    assert "Beacon identity" in response.text


def test_beacon_setup_page_prefills_known_values(client, conn):
    _configure_beacon(conn)

    response = client.get("/beacon")

    assert "CD3DXZ-1" in response.text
    assert "FF46vb" in response.text


def test_beacon_setup_page_never_falls_back_to_env(client, conn, monkeypatch):
    monkeypatch.setenv("BEACON_CALLSIGN", "SHOULD-NOT-APPEAR")

    response = client.get("/beacon")

    assert "SHOULD-NOT-APPEAR" not in response.text


def test_beacon_setup_page_shows_not_configured_badge(client):
    response = client.get("/beacon")

    assert "not configured" in response.text


def test_beacon_setup_page_shows_configured_badge(client, conn):
    _configure_beacon(conn)

    response = client.get("/beacon")

    assert "not configured" not in response.text


# --- POST /beacon ---


def test_beacon_setup_save_persists_and_redirects(client, conn):
    response = client.post("/beacon", data=ALL_VALUES, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/beacon")
    assert get_setting("BEACON_CALLSIGN", conn=conn, env_fallback=False) == "CD3DXZ-1"


def test_beacon_setup_save_rejects_blank_required_field(client):
    incomplete = dict(ALL_VALUES, BEACON_CALLSIGN="")

    response = client.post("/beacon", data=incomplete, follow_redirects=False)

    assert response.status_code == 400
    assert "Required" in response.text
    assert "Callsign" in response.text
    # Already-entered fields are preserved in the re-rendered form.
    assert "operator@example.com" in response.text


def test_beacon_setup_save_rejects_whitespace_only_field(client):
    incomplete = dict(ALL_VALUES, BEACON_CALLSIGN="   ")

    response = client.post("/beacon", data=incomplete, follow_redirects=False)

    assert response.status_code == 400


def test_beacon_setup_save_does_not_persist_on_validation_failure(client, conn):
    incomplete = dict(ALL_VALUES, BEACON_CALLSIGN="")

    client.post("/beacon", data=incomplete, follow_redirects=False)

    assert is_beacon_configured(conn) is False


# --- sitewide banner / nav badge ---


def test_sitewide_banner_shown_when_not_configured(client):
    response = client.get("/")

    assert "Beacon identity not configured" in response.text


def test_sitewide_banner_hidden_once_configured(client, conn):
    _configure_beacon(conn)

    response = client.get("/")

    assert "Beacon identity not configured" not in response.text


def test_sitewide_banner_hidden_on_beacon_page_itself(client):
    """The setup page already shows its own "not configured" badge in the
    page header — repeating the sitewide banner there would be redundant."""
    response = client.get("/beacon")

    assert "Beacon identity not configured" not in response.text


def test_nav_badge_shown_when_not_configured(client):
    response = client.get("/")

    assert 'href="/beacon"' in response.text
    assert "badge-warn" in response.text


def test_nav_badge_hidden_once_configured(client, conn):
    _configure_beacon(conn)

    response = client.get("/")

    # The nav link itself is always present; only its "!" badge goes away.
    assert 'href="/beacon"' in response.text
    assert "badge-warn" not in response.text


def test_all_catalog_fields_covered_by_test_values():
    """Guards against BEACON_FIELDS drifting out of sync with this test
    file's ALL_VALUES fixture."""
    assert {f.key for f in BEACON_FIELDS} == set(ALL_VALUES)
