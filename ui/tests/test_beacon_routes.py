from adapters.storage import get_setting, set_beacon_status, set_setting

from ui.beacon import is_beacon_configured
from ui.config_catalog import specs_for_group
from ui.routers.beacon import _queue_bar, _status_context

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


# --- GET /config/beacon-identity ---


def test_beacon_identity_page_returns_200(client):
    response = client.get("/config/beacon-identity")

    assert response.status_code == 200
    assert "Beacon — Identity" in response.text


def test_beacon_identity_page_prefills_known_values(client, conn):
    _configure_beacon(conn)

    response = client.get("/config/beacon-identity")

    assert "CD3DXZ-1" in response.text
    assert "FF46vb" in response.text


def test_beacon_identity_page_never_falls_back_to_env(client, conn, monkeypatch):
    monkeypatch.setenv("BEACON_CALLSIGN", "SHOULD-NOT-APPEAR")

    response = client.get("/config/beacon-identity")

    assert "SHOULD-NOT-APPEAR" not in response.text


# --- POST /config/beacon-identity ---


def test_beacon_identity_save_persists_and_redirects(client, conn):
    response = client.post("/config/beacon-identity", data=ALL_VALUES, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/config/beacon-identity")
    assert get_setting("BEACON_CALLSIGN", conn=conn, env_fallback=False) == "CD3DXZ-1"


def test_beacon_identity_save_rejects_blank_required_field(client):
    incomplete = dict(ALL_VALUES, BEACON_CALLSIGN="")

    response = client.post("/config/beacon-identity", data=incomplete, follow_redirects=False)

    assert response.status_code == 400
    assert "Required" in response.text
    assert "Callsign" in response.text
    # Already-entered fields are preserved in the re-rendered form.
    assert "operator@example.com" in response.text


def test_beacon_identity_save_rejects_whitespace_only_field(client):
    incomplete = dict(ALL_VALUES, BEACON_CALLSIGN="   ")

    response = client.post("/config/beacon-identity", data=incomplete, follow_redirects=False)

    assert response.status_code == 400


def test_beacon_identity_save_does_not_persist_on_validation_failure(client, conn):
    incomplete = dict(ALL_VALUES, BEACON_CALLSIGN="")

    client.post("/config/beacon-identity", data=incomplete, follow_redirects=False)

    assert is_beacon_configured(conn) is False


# --- sitewide banner / nav badge ---


def test_sitewide_banner_shown_when_not_configured(client):
    response = client.get("/")

    assert "Beacon identity not configured" in response.text


def test_sitewide_banner_hidden_once_configured(client, conn):
    _configure_beacon(conn)

    response = client.get("/")

    assert "Beacon identity not configured" not in response.text


def test_sitewide_banner_hidden_on_beacon_identity_page_itself(client):
    """The generic config form already asterisks/marks required fields —
    repeating the sitewide banner there would be redundant."""
    response = client.get("/config/beacon-identity")

    assert "Beacon identity not configured" not in response.text


_NAV_BADGE = 'Config <span class="badge badge-warn">!</span>'


def test_nav_badge_shown_when_not_configured(client):
    response = client.get("/")

    assert 'href="/config"' in response.text
    assert _NAV_BADGE in response.text


def test_nav_badge_hidden_once_configured(client, conn):
    _configure_beacon(conn)

    response = client.get("/")

    # The nav link itself is always present; only its "!" badge goes away.
    assert 'href="/config"' in response.text
    assert _NAV_BADGE not in response.text


def test_all_catalog_fields_covered_by_test_values():
    """Guards against the "Beacon — Identity" catalog group drifting out
    of sync with this test file's ALL_VALUES fixture."""
    assert {spec.key for spec in specs_for_group("beacon-identity")} == set(ALL_VALUES)


# --- dashboard beacon section / enable / disable ---


def test_dashboard_shows_disabled_by_default(client):
    response = client.get("/")

    assert "disabled" in response.text


def test_dashboard_shows_not_running_with_no_heartbeat(client):
    response = client.get("/")

    assert "not running" in response.text


def test_dashboard_shows_running_with_recent_heartbeat(client, conn):
    from datetime import datetime, timezone

    set_beacon_status(conn, "process_heartbeat_at", datetime.now(timezone.utc).isoformat())

    response = client.get("/")

    assert "not running" not in response.text


def test_dashboard_shows_not_running_with_stale_heartbeat(client, conn):
    from datetime import datetime, timedelta, timezone

    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    set_beacon_status(conn, "process_heartbeat_at", stale.isoformat())

    response = client.get("/")

    assert "not running" in response.text


def test_dashboard_shows_pending_transmits_for_active_type(client, conn):
    # BEACON_TYPE defaults to "voice".
    set_beacon_status(conn, "voice_queue_depth", "3")
    set_beacon_status(conn, "frame_queue_depth", "1")

    response = client.get("/")

    # Only the active type's depth is shown in the Queue status tile; max
    # defaults to 200 when unset.
    assert '3 <span class="muted">/ 200</span>' in response.text
    assert '1 <span class="muted">/ 200</span>' not in response.text


def test_dashboard_shows_beacon_type(client, conn):
    set_setting(conn, "BEACON_TYPE", "frame")
    set_beacon_status(conn, "beacon_type", "frame")
    set_beacon_status(conn, "frame_queue_depth", "2")

    response = client.get("/")

    assert "frame" in response.text
    assert '2 <span class="muted">/ 200</span>' in response.text


def test_beacon_enable_action_sets_flag_and_redirects(client, conn):
    response = client.post("/beacon/enable", follow_redirects=False)

    assert response.status_code == 303
    assert get_setting("BEACON_ENABLED", conn=conn) == "true"


def test_beacon_disable_action_sets_flag_and_redirects(client, conn):
    set_setting(conn, "BEACON_ENABLED", "true")

    response = client.post("/beacon/disable", follow_redirects=False)

    assert response.status_code == 303
    assert get_setting("BEACON_ENABLED", conn=conn) == "false"


def test_dashboard_reflects_beacon_enabled_state(client, conn):
    set_setting(conn, "BEACON_ENABLED", "true")

    response = client.get("/")

    # Beacon side panel badge + the quick-control switch reflect the state.
    assert '<span class="badge badge-success">enabled</span>' in response.text
    assert 'name="key" value="BEACON_ENABLED"' in response.text


def test_dashboard_mode_switch_reflects_setting_not_stale_telemetry(client, conn):
    # Beacon process last reported "frame"; operator switches the setting
    # to "voice". The control (and the whole live region) must follow the
    # setting immediately, and flag the drift.
    set_beacon_status(conn, "beacon_type", "frame")
    set_setting(conn, "BEACON_TYPE", "voice")

    fragment = client.get("/", headers={"X-Auto-Refresh": "1"}).text

    assert 'data-cell="quick_controls"' in fragment
    assert '<dd data-cell="bp_type">voice</dd>' in fragment
    assert "still frame" in fragment  # drift hint


def test_dashboard_quick_toggle_flips_a_live_setting(client, conn):
    set_setting(conn, "ACTIONS_AI_ENABLED", "false")

    response = client.post(
        "/dashboard/toggle",
        data={"key": "ACTIONS_AI_ENABLED", "value": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_setting("ACTIONS_AI_ENABLED", conn=conn) == "true"


def test_dashboard_quick_toggle_rejects_key_not_on_allowlist(client, conn):
    response = client.post(
        "/dashboard/toggle",
        data={"key": "ANTHROPIC_API_KEY", "value": "leaked"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert get_setting("ANTHROPIC_API_KEY", "", conn=conn) == ""


def test_dashboard_quick_toggle_ajax_returns_json_with_a_patched_fragment(client, conn):
    # ajax-forms.js marks a fetch-driven POST with X-Requested-With: fetch —
    # the route must skip the redirect and answer with the envelope it
    # patches [data-cell] regions from instead of navigating.
    set_setting(conn, "ACTIONS_AI_ENABLED", "false")

    response = client.post(
        "/dashboard/toggle",
        data={"key": "ACTIONS_AI_ENABLED", "value": "true"},
        headers={"X-Requested-With": "fetch"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "AI summarization" in body["message"]
    assert 'data-cell="quick_controls"' in body["fragment"]
    assert get_setting("ACTIONS_AI_ENABLED", conn=conn) == "true"


def test_dashboard_quick_toggle_ajax_rejects_key_not_on_allowlist(client, conn):
    response = client.post(
        "/dashboard/toggle",
        data={"key": "ANTHROPIC_API_KEY", "value": "leaked"},
        headers={"X-Requested-With": "fetch"},
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert get_setting("ANTHROPIC_API_KEY", "", conn=conn) == ""


def test_beacon_group_settings_appear_in_config(client):
    # The category landing page lists groups (linking to their edit form),
    # not individual keys — see BEACON_TYPE on the group's own form instead.
    response = client.get("/config/beacon")
    assert "Beacon — Transmission" in response.text
    assert 'href="/config/beacon-transmission"' in response.text

    group_page = client.get("/config/beacon-transmission")
    assert "BEACON_TYPE" in group_page.text


def test_beacon_transmission_group_editable_via_config(client, conn):
    response = client.post(
        "/config/beacon-transmission",
        data={"BEACON_TYPE": "frame"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_setting("BEACON_TYPE", conn=conn) == "frame"


# --- _status_context ---


def test_status_context_defaults_to_voice_type(conn):
    result = _status_context(conn)

    assert result["beacon_type"] == "voice"
    assert result["running"] is False
    assert result["queue_depth"] == 0


def test_status_context_reports_active_type_queue_depth(conn):
    set_setting(conn, "BEACON_TYPE", "frame")
    set_beacon_status(conn, "frame_queue_depth", "4")
    set_beacon_status(conn, "voice_queue_depth", "9")

    result = _status_context(conn)

    assert result["beacon_type"] == "frame"
    assert result["queue_depth"] == 4


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
