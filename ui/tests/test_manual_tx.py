"""Dashboard "Transmit now" — POST /dashboard/transmit + the modal."""
from adapters.storage import (
    count_manual_tx_by_kind,
    pending_manual_tx,
    set_setting,
)


def _configure_beacon(conn):
    for key, value in {
        "BEACON_CALLSIGN": "CD3DXZ-1",
        "BEACON_DESCRIPTION": "d",
        "BEACON_SHORT_DESCRIPTION": "s",
        "BEACON_OPERATOR_CONTACT": "o@example.com",
        "BEACON_GRID_LOCATOR": "FF46vb",
        "BEACON_FREQUENCY": "144.390 MHz",
    }.items():
        set_setting(conn, key, value)


def test_transmit_blocked_until_beacon_configured(client, conn):
    resp = client.post(
        "/dashboard/transmit", data={"kind": "voice", "text": "hola"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert count_manual_tx_by_kind(conn) == {}


def test_transmit_queues_voice_message(client, conn):
    _configure_beacon(conn)
    resp = client.post(
        "/dashboard/transmit",
        data={"kind": "voice", "text": "  Prueba de transmisión  "},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "msg=" in resp.headers["location"]
    rows = pending_manual_tx(conn, "voice")
    assert len(rows) == 1
    assert rows[0]["text"] == "Prueba de transmisión"  # trimmed
    assert rows[0]["actor"] == "ui.dashboard"


def test_transmit_rejects_unknown_kind(client, conn):
    _configure_beacon(conn)
    resp = client.post(
        "/dashboard/transmit", data={"kind": "cw", "text": "hi"}, follow_redirects=False
    )
    assert "error=" in resp.headers["location"]
    assert count_manual_tx_by_kind(conn) == {}


def test_transmit_rejects_empty_message(client, conn):
    _configure_beacon(conn)
    resp = client.post(
        "/dashboard/transmit", data={"kind": "voice", "text": "   "}, follow_redirects=False
    )
    assert "error=" in resp.headers["location"]


def test_transmit_rejects_voice_over_char_limit(client, conn):
    _configure_beacon(conn)
    set_setting(conn, "BEACON_VOICE_MAX_CHARS", "20")
    resp = client.post(
        "/dashboard/transmit", data={"kind": "voice", "text": "x" * 21}, follow_redirects=False
    )
    assert "error=" in resp.headers["location"]
    assert count_manual_tx_by_kind(conn) == {}


def test_transmit_rejects_frame_over_byte_limit(client, conn):
    _configure_beacon(conn)
    resp = client.post(
        "/dashboard/transmit", data={"kind": "frame", "text": "x" * 400}, follow_redirects=False
    )
    assert "error=" in resp.headers["location"]
    assert count_manual_tx_by_kind(conn) == {}


def test_dashboard_shows_transmit_button_and_dialog(client, conn):
    _configure_beacon(conn)
    body = client.get("/").text
    assert 'data-open-dialog="manual-tx-dialog"' in body
    assert 'id="manual-tx-dialog"' in body
    assert 'action="/dashboard/transmit"' in body


def test_dashboard_shows_queued_count(client, conn):
    _configure_beacon(conn)
    client.post("/dashboard/transmit", data={"kind": "voice", "text": "uno"})
    body = client.get("/").text
    assert "1 queued" in body
