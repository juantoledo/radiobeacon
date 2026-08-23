from adapters.storage import get_setting, set_beacon_status, set_setting

from ui.beacon import BEACON_FIELDS, is_beacon_configured
from ui.routers.beacon import _format_duration, _queue_bar, _timeline_context

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


# --- status section / enable / disable ---


def test_beacon_page_shows_disabled_by_default(client):
    response = client.get("/beacon")

    assert "disabled" in response.text


def test_beacon_page_shows_not_running_with_no_heartbeat(client):
    response = client.get("/beacon")

    assert "not running" in response.text


def test_beacon_page_shows_running_with_recent_heartbeat(client, conn):
    from datetime import datetime, timezone

    set_beacon_status(conn, "process_heartbeat_at", datetime.now(timezone.utc).isoformat())

    response = client.get("/beacon")

    assert "not running" not in response.text


def test_beacon_page_shows_not_running_with_stale_heartbeat(client, conn):
    from datetime import datetime, timedelta, timezone

    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    set_beacon_status(conn, "process_heartbeat_at", stale.isoformat())

    response = client.get("/beacon")

    assert "not running" in response.text


def test_beacon_page_shows_queue_depths(client, conn):
    set_beacon_status(conn, "voice_queue_depth", "3")
    set_beacon_status(conn, "frame_queue_depth", "1")

    response = client.get("/beacon")

    # BEACON_QUEUE_MAX_SIZE defaults to 200 when unset.
    assert '3<span class="muted"> / 200</span>' in response.text
    assert '1<span class="muted"> / 200</span>' in response.text


def test_beacon_page_shows_current_slot(client, conn):
    set_beacon_status(conn, "current_slot", "voice")

    response = client.get("/beacon")

    assert "voice" in response.text


def test_beacon_page_shows_time_left_when_running(client, conn):
    from datetime import datetime, timezone

    set_beacon_status(conn, "process_heartbeat_at", datetime.now(timezone.utc).isoformat())
    set_beacon_status(conn, "current_slot_remaining_seconds", "47.6")

    response = client.get("/beacon")

    assert "48s" in response.text


def test_beacon_page_hides_time_left_when_not_running(client, conn):
    """A frozen countdown from a stale heartbeat would actively mislead —
    unlike the last-known slot name, which stays informative even stale."""
    set_beacon_status(conn, "current_slot_remaining_seconds", "47.6")

    response = client.get("/beacon")

    assert "48s" not in response.text


def test_beacon_page_shows_cycle_timeline_segments(client, conn):
    response = client.get("/beacon")

    # Defaults: total=90, voice=60, guard=0 (omitted since 0s), frame=30.
    assert "voice · 60s" in response.text
    assert "frame · 30s" in response.text
    assert "slot-guard" not in response.text


def test_beacon_page_timeline_falls_back_when_window_misconfigured(client, conn):
    set_setting(conn, "BEACON_WINDOW_VOICE_SECONDS", "9999")  # exceeds total

    response = client.get("/beacon")

    assert response.status_code == 200
    assert "Cycle timeline unavailable" in response.text


def test_beacon_page_loads_refresh_script_with_configured_interval(client):
    response = client.get("/beacon")

    assert 'src="/static/dashboard-refresh.js"' in response.text
    assert 'data-interval-ms="2000"' in response.text  # UI_BEACON_REFRESH_SECONDS default 2


def test_beacon_enable_action_sets_flag_and_redirects(client, conn):
    response = client.post("/beacon/enable", follow_redirects=False)

    assert response.status_code == 303
    assert get_setting("BEACON_ENABLED", conn=conn) == "true"


def test_beacon_disable_action_sets_flag_and_redirects(client, conn):
    set_setting(conn, "BEACON_ENABLED", "true")

    response = client.post("/beacon/disable", follow_redirects=False)

    assert response.status_code == 303
    assert get_setting("BEACON_ENABLED", conn=conn) == "false"


def test_beacon_page_reflects_enabled_state(client, conn):
    set_setting(conn, "BEACON_ENABLED", "true")

    response = client.get("/beacon")

    assert ">Disable<" in response.text  # button offers the opposite action


def test_beacon_group_settings_appear_in_config(client):
    response = client.get("/config")

    assert "Beacon — Schedule" in response.text
    assert "BEACON_WINDOW_TOTAL_SECONDS" in response.text


def test_beacon_schedule_group_editable_via_config(client, conn):
    response = client.post(
        "/config/beacon-schedule",
        data={"BEACON_WINDOW_TOTAL_SECONDS": "120"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_setting("BEACON_WINDOW_TOTAL_SECONDS", conn=conn) == "120"


# --- _format_duration ---


def test_format_duration_under_a_minute():
    assert _format_duration(47.6) == "48s"


def test_format_duration_rounds_to_whole_seconds():
    assert _format_duration(0.4) == "0s"


def test_format_duration_over_a_minute():
    assert _format_duration(125.0) == "2m 5s"


# --- _queue_bar ---


def test_queue_bar_pct_and_no_fill_class_when_low():
    result = _queue_bar(depth=2, max_size=20)

    assert result["pct"] == 10.0
    assert result["fill_class"] == ""


def test_queue_bar_warn_fill_class_at_60_percent():
    result = _queue_bar(depth=12, max_size=20)

    assert result["fill_class"] == "fill-warn"


def test_queue_bar_danger_fill_class_at_85_percent():
    result = _queue_bar(depth=18, max_size=20)

    assert result["fill_class"] == "fill-danger"


def test_queue_bar_clamps_to_100_when_over_capacity():
    result = _queue_bar(depth=25, max_size=20)

    assert result["pct"] == 100.0


def test_queue_bar_handles_zero_max_size():
    result = _queue_bar(depth=5, max_size=0)

    assert result["pct"] == 0.0


# --- _timeline_context ---


def test_timeline_context_default_window_segments_sum_to_total(conn):
    result = _timeline_context(conn, {})

    assert result["timeline_valid"] is True
    total_pct = sum(seg["pct"] for seg in result["segments"])
    assert round(total_pct, 5) == 100.0  # voice + frame + idle (guard=0s omitted)


def test_timeline_context_omits_zero_length_guard_segment(conn):
    # Defaults: total=90, voice=60, guard=0, frame=30 -- idle is also 0
    # (60+0+30 == 90 exactly), so only voice/frame are non-zero-length.
    result = _timeline_context(conn, {})

    slots = [seg["slot"] for seg in result["segments"]]
    assert "guard" not in slots
    assert slots == ["voice", "frame"]


def test_timeline_context_marker_position_from_elapsed_seconds(conn):
    result = _timeline_context(conn, {"current_cycle_elapsed_seconds": "45"})

    assert result["marker_pct"] == 50.0  # 45 / 90 total


def test_timeline_context_marker_none_when_no_elapsed_status(conn):
    result = _timeline_context(conn, {})

    assert result["marker_pct"] is None


def test_timeline_context_invalid_when_slots_exceed_total(conn):
    set_setting(conn, "BEACON_WINDOW_VOICE_SECONDS", "9999")

    result = _timeline_context(conn, {})

    assert result["timeline_valid"] is False
    assert result["segments"] == []


def test_timeline_context_invalid_when_total_is_zero(conn):
    set_setting(conn, "BEACON_WINDOW_TOTAL_SECONDS", "0")

    result = _timeline_context(conn, {})

    assert result["timeline_valid"] is False
