"""Dashboard "Transmit now" — POST /dashboard/transmit + the modal +
playback of the rendered manual clips."""
from adapters.storage import (
    count_manual_tx_by_kind,
    pending_manual_tx,
    record_audit_event,
    set_setting,
)

from ui import beacon_audio


def _configure_beacon(conn):
    for key, value in {
        "BEACON_CALLSIGN": "N0CALL-1",
        "BEACON_DESCRIPTION": "d",
        "BEACON_SHORT_DESCRIPTION": "s",
        "BEACON_OPERATOR_CONTACT": "o@example.com",
        "BEACON_GRID_LOCATOR": "FF46vb",
        "BEACON_FREQUENCY": "TX frequency",
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


# --- playback of rendered manual clips ---


def test_manual_clip_path_rejects_bad_names(conn, tmp_path):
    set_setting(conn, "BEACON_TTS_WAV_DIR", str(tmp_path))
    assert beacon_audio.manual_clip_path(conn, "../secret.wav") is None
    assert beacon_audio.manual_clip_path(conn, "csn-1-2.wav") is None
    assert beacon_audio.manual_clip_path(conn, "manual-1-2.wav") is None  # no file on disk


def test_manual_audio_route_serves_and_404s(client, conn, tmp_path):
    _configure_beacon(conn)
    set_setting(conn, "BEACON_TTS_WAV_DIR", str(tmp_path))
    (tmp_path / "manual-7-1788400000.wav").write_bytes(b"RIFFmanual-bytes")

    ok = client.get("/dashboard/manual-audio/manual-7-1788400000.wav")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "audio/wav"
    assert ok.content == b"RIFFmanual-bytes"

    assert client.get("/dashboard/manual-audio/manual-9-1788400000.wav").status_code == 404
    assert client.get("/dashboard/manual-audio/etc-passwd.wav").status_code == 404


def test_dashboard_audit_feed_shows_play_button_for_manual_transmit_with_clip(client, conn, tmp_path):
    _configure_beacon(conn)
    set_setting(conn, "BEACON_TTS_WAV_DIR", str(tmp_path))
    (tmp_path / "manual-3-1788400000.wav").write_bytes(b"RIFF")
    record_audit_event(
        conn, event_type="beacon.manual.transmitted", actor="beacon",
        details={"kind": "voice", "chars": 4, "manual_id": 3, "clip": "manual-3-1788400000.wav"},
    )

    body = client.get("/").text
    assert "beacon.manual.transmitted" in body
    assert 'data-audio-url="/dashboard/manual-audio/manual-3-1788400000.wav"' in body


def test_dashboard_audit_feed_hides_play_button_when_clip_missing(client, conn, tmp_path):
    _configure_beacon(conn)
    set_setting(conn, "BEACON_TTS_WAV_DIR", str(tmp_path))
    record_audit_event(
        conn, event_type="beacon.manual.transmitted", actor="beacon",
        details={"kind": "voice", "chars": 4, "manual_id": 9, "clip": "manual-9-1788400000.wav"},
    )

    body = client.get("/").text
    assert "beacon.manual.transmitted" in body
    assert "data-audio-url=\"/dashboard/manual-audio/manual-9-1788400000.wav\"" not in body


def test_dashboard_items_feed_shows_manual_transmit_with_play_button(client, conn, tmp_path):
    _configure_beacon(conn)
    set_setting(conn, "BEACON_TTS_WAV_DIR", str(tmp_path))
    (tmp_path / "manual-3-1788400000.wav").write_bytes(b"RIFF")
    record_audit_event(
        conn, event_type="beacon.manual.transmitted", actor="beacon",
        details={"kind": "voice", "chars": 4, "manual_id": 3, "clip": "manual-3-1788400000.wav"},
    )

    body = client.get("/").text
    assert "Manual transmission" in body
    assert 'data-audio-url="/dashboard/manual-audio/manual-3-1788400000.wav"' in body


def test_dashboard_items_feed_hides_play_button_when_manual_clip_missing(client, conn, tmp_path):
    _configure_beacon(conn)
    set_setting(conn, "BEACON_TTS_WAV_DIR", str(tmp_path))
    record_audit_event(
        conn, event_type="beacon.manual.transmitted", actor="beacon",
        details={"kind": "voice", "chars": 4, "manual_id": 9, "clip": "manual-9-1788400000.wav"},
    )

    body = client.get("/").text
    assert "Manual transmission" in body
    assert "manual-9-1788400000.wav" not in body
