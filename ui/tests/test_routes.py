from adapters.storage import record_audit_event, set_setting
from adapters.policy import set_policy
from ui.audit_ack import AUDIT_ACK_COOKIE_NAME


def _insert_item(conn, source, item_id, *, policy="informational", source_date_time=None):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, fetched_at, source_date_time, policy, rawdata) "
        "VALUES (?, ?, 'Title', datetime('now'), ?, ?, '{}')",
        (source, item_id, source_date_time, policy),
    )
    conn.commit()


def _configure_beacon(conn):
    """Satisfies is_beacon_configured() so override/rearm actions aren't
    blocked — see config_catalog.py's "Beacon — Identity" group for the
    full required set."""
    for key in (
        "BEACON_CALLSIGN",
        "BEACON_DESCRIPTION",
        "BEACON_SHORT_DESCRIPTION",
        "BEACON_OPERATOR_CONTACT",
        "BEACON_GRID_LOCATOR",
        "BEACON_FREQUENCY",
    ):
        set_setting(conn, key, "test-value")


def test_dashboard_returns_200(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Dashboard" in response.text


def test_dashboard_failed_events_banner_links_to_prefiltered_audit_log(client, conn):
    record_audit_event(conn, event_type="item.dispatch_failed", actor="test")

    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/audit?status=failed&since=24h"' in response.text


def test_dashboard_banner_hides_after_ack_and_reappears_for_new_failures(client, conn):
    record_audit_event(conn, event_type="item.dispatch_failed", actor="test")
    assert "check the audit log" in client.get("/").text

    # Following the banner's own link (status=failed) acknowledges every
    # failure recorded so far.
    ack = client.get("/audit", params={"status": "failed", "since": "24h"})
    assert AUDIT_ACK_COOKIE_NAME in ack.cookies

    assert "check the audit log" not in client.get("/").text

    record_audit_event(conn, event_type="beacon.voice.transmit_failed", actor="test")
    assert "check the audit log" in client.get("/").text


def test_dashboard_includes_auto_refresh_script_when_enabled(client, conn):
    set_setting(conn, "UI_DASHBOARD_REFRESH_SECONDS", "5")

    response = client.get("/")

    assert 'id="dashboard-content"' in response.text
    assert "dashboard-refresh.js" in response.text
    assert 'data-interval-ms="5000"' in response.text


def test_dashboard_activity_tabs_are_css_only(client):
    """Items/Audit switching must not depend on dashboard-refresh.js (which
    is cache-sensitive and only loaded when auto-refresh is on)."""
    text = client.get("/").text

    assert 'id="af-items"' in text and 'id="af-audit"' in text
    assert 'for="af-audit"' in text
    assert "data-feed-tab" not in text  # old JS-driven markup is gone


def test_static_urls_are_cache_busted(client):
    text = client.get("/").text

    assert "/static/style.css?v=" in text


def test_dashboard_live_fragment_is_partial_html(client):
    full = client.get("/").text
    fragment = client.get("/", headers={"X-Auto-Refresh": "1"}).text

    assert 'id="dashboard-content"' in fragment
    assert "<!doctype html>" not in fragment.lower()
    assert "<!doctype html>" in full.lower()
    assert 'data-cell="kpi_items"' in fragment


def test_dashboard_omits_auto_refresh_script_when_disabled(client, conn):
    set_setting(conn, "UI_DASHBOARD_REFRESH_SECONDS", "0")

    response = client.get("/")

    assert "dashboard-refresh.js" not in response.text


def test_dashboard_items_feed_includes_watermark_transmissions(client, conn):
    """A watermark has no `items` row of its own (no source/item_id) -- it
    should still show up on the Items tab as a plain entry, interleaved by
    time with real items, since its WAV is deleted right after transmit and
    there's nothing to link to or play."""
    _insert_item(conn, "senapred", "1")
    record_audit_event(
        conn, event_type="beacon.watermark.transmitted", actor="beacon",
        details={"clip": "watermark-1.wav", "kind": "voice"},
    )

    text = client.get("/").text

    assert "Watermark transmission" in text
    assert "watermark-1.wav" not in text


def test_dashboard_items_feed_orders_watermark_correctly_against_same_day_items(client, conn):
    """Regression test: items.source_date_time is stored as Python's
    offset-suffixed ISO 8601 ("...T10:00:00+00:00"), while audit_log's
    recorded_at is SQLite's offset-less datetime('now') ("...  10:00:00").
    Comparing those two shapes as plain strings sorts the space before "T",
    so a watermark recorded "now" would rank as older than any item dated
    earlier the same day, and fall out of the merged feed's limit=8 window
    even though it's chronologically the newest entry."""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for i in range(8):
        _insert_item(conn, "senapred", str(i), source_date_time=f"{today}T0{i}:00:00+00:00")
    record_audit_event(
        conn, event_type="beacon.watermark.transmitted", actor="beacon",
        details={"clip": "watermark-1.wav", "kind": "voice"},
    )

    text = client.get("/").text

    assert "Watermark transmission" in text


def test_dashboard_refresh_script_is_served_and_non_empty(client):
    response = client.get("/static/dashboard-refresh.js")

    assert response.status_code == 200
    assert "dashboard-content" in response.text


def test_items_list_returns_200_when_empty(client):
    response = client.get("/items")
    assert response.status_code == 200
    assert "No items match" in response.text


def test_item_detail_returns_404_for_unknown_item(client):
    response = client.get("/items/csn/does-not-exist")
    assert response.status_code == 404


def test_item_detail_returns_200_for_known_item(client, conn):
    _insert_item(conn, "csn", "1")

    response = client.get("/items/csn/1")

    assert response.status_code == 200
    assert "Title" in response.text


def test_override_action_redirects_and_updates_row(client, conn):
    _configure_beacon(conn)
    _insert_item(conn, "csn", "1", policy="informational")

    response = client.post(
        "/items/csn/1/override", data={"policy": "urgent"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/items/csn/1")
    row = conn.execute(
        "SELECT policy FROM items WHERE source='csn' AND item_id='1'"
    ).fetchone()
    assert row[0] == "urgent"


def test_override_action_missing_policy_is_rejected(client, conn):
    _configure_beacon(conn)
    _insert_item(conn, "csn", "1")

    response = client.post("/items/csn/1/override", data={}, follow_redirects=False)

    assert response.status_code == 422


def test_override_action_blocked_when_beacon_not_configured(client, conn):
    _insert_item(conn, "csn", "1", policy="informational")

    response = client.post(
        "/items/csn/1/override", data={"policy": "urgent"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    row = conn.execute(
        "SELECT policy FROM items WHERE source='csn' AND item_id='1'"
    ).fetchone()
    assert row[0] == "informational"


def test_rearm_action_redirects(client, conn):
    _configure_beacon(conn)
    _insert_item(conn, "csn", "1")

    response = client.post(
        "/items/csn/1/rearm", data={"consumer": "log"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/items/csn/1")


def test_rearm_action_blocked_when_beacon_not_configured(client, conn):
    _insert_item(conn, "csn", "1")

    response = client.post(
        "/items/csn/1/rearm", data={"consumer": "log"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_replay_action_redirects_for_dispatch_stage_event(client, conn):
    _configure_beacon(conn)
    _insert_item(conn, "csn", "1")

    response = client.post(
        "/items/csn/1/replay",
        data={"event_type": "item.dispatched", "consumer": "log"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "msg=" in response.headers["location"]
    row = conn.execute(
        "SELECT 1 FROM trigger_dispatches WHERE consumer='log' AND source='csn' AND item_id='1'"
    ).fetchone()
    assert row is not None


def test_replay_action_rejects_non_replayable_event_type(client, conn):
    _configure_beacon(conn)
    _insert_item(conn, "csn", "1")

    response = client.post(
        "/items/csn/1/replay",
        data={"event_type": "action.ai.executed", "consumer": "log"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    row = conn.execute(
        "SELECT 1 FROM trigger_dispatches WHERE consumer='log' AND source='csn' AND item_id='1'"
    ).fetchone()
    assert row is None


def test_replay_action_blocked_when_beacon_not_configured(client, conn):
    _insert_item(conn, "csn", "1")

    response = client.post(
        "/items/csn/1/replay",
        data={"event_type": "item.dispatched", "consumer": "log"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_retransmit_action_redirects_and_schedules_row(client, conn):
    _configure_beacon(conn)
    _insert_item(conn, "csn", "1")

    response = client.post("/items/csn/1/retransmit", follow_redirects=False)

    assert response.status_code == 303
    assert "msg=" in response.headers["location"]
    row = conn.execute(
        "SELECT kind FROM beacon_tx_schedule WHERE source='csn' AND item_id='1'"
    ).fetchone()
    assert row[0] == "voice"


def test_retransmit_action_unknown_item(client, conn):
    _configure_beacon(conn)

    response = client.post("/items/csn/does-not-exist/retransmit", follow_redirects=False)

    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_retransmit_action_blocked_when_beacon_not_configured(client, conn):
    _insert_item(conn, "csn", "1")

    response = client.post("/items/csn/1/retransmit", follow_redirects=False)

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    row = conn.execute(
        "SELECT 1 FROM beacon_tx_schedule WHERE source='csn' AND item_id='1'"
    ).fetchone()
    assert row is None


def test_policies_list_returns_200(client):
    response = client.get("/config/policies")
    assert response.status_code == 200
    # a fresh install seeds only `default` (see adapters.policy)
    assert "default" in response.text


def test_legacy_policies_url_redirects_under_config(client):
    response = client.get("/policies", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/config/policies"


def test_policy_create_redirects_and_persists(client, conn):
    response = client.post(
        "/config/policies",
        data={
            "name": "critical",
            "fetch_kind": "interval",
            "fetch_interval_seconds": "10",
            "transmit_kind": "interval",
            "transmit_count": "10",
            "transmit_interval_seconds": "30",
            "description": "test policy",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/config/policies")
    row = conn.execute(
        "SELECT transmit_count, transmit_interval_seconds FROM policies WHERE name='critical'"
    ).fetchone()
    assert tuple(row) == (10, 30)


def test_policy_edit_page_returns_200_for_known_policy(client, conn):
    set_policy(conn, "custom", transmit_kind="interval", transmit_count=2, transmit_interval_seconds=15, description="desc")

    response = client.get("/config/policies/custom/edit")

    assert response.status_code == 200
    assert "custom" in response.text


def test_policy_edit_page_returns_404_for_unknown_policy(client):
    response = client.get("/config/policies/does-not-exist/edit")
    assert response.status_code == 404


def test_policy_delete_redirects(client, conn):
    set_policy(conn, "temp")

    response = client.post("/config/policies/temp/delete", follow_redirects=False)

    assert response.status_code == 303
    row = conn.execute("SELECT 1 FROM policies WHERE name='temp'").fetchone()
    assert row is None


def test_policy_delete_ajax_returns_fragment_without_deleted_row(client, conn):
    set_policy(conn, "temp")

    response = client.post(
        "/config/policies/temp/delete", headers={"X-Requested-With": "fetch"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "fragment" in body
    assert 'data-cell="policies-rows"' in body["fragment"]
    assert ">temp<" not in body["fragment"]
    row = conn.execute("SELECT 1 FROM policies WHERE name='temp'").fetchone()
    assert row is None


def test_audit_log_returns_200(client):
    response = client.get("/audit")
    assert response.status_code == 200
    assert "No audit events match" in response.text


def test_audit_log_filters_by_event_type(client, conn):
    _insert_item(conn, "csn", "1")
    conn.execute(
        "INSERT INTO audit_log (event_type, actor, source, item_id) "
        "VALUES ('item.stored', 'test', 'csn', '1')"
    )
    conn.commit()

    response = client.get("/audit", params={"event_type": "item.stored"})

    assert response.status_code == 200
    assert "item.stored" in response.text


def test_audit_log_status_failed_filters_to_failures_only(client, conn):
    conn.execute(
        "INSERT INTO audit_log (event_type, actor) VALUES ('item.dispatch_failed', 'test')"
    )
    conn.execute("INSERT INTO audit_log (event_type, actor) VALUES ('item.stored', 'test')")
    conn.commit()

    response = client.get("/audit", params={"status": "failed"})

    assert response.status_code == 200
    # "item.stored" still legitimately appears in the event_type <select>'s
    # option list (populated from all distinct event types, not the
    # filtered result set) — the table body itself is what must be scoped
    # to failures only.
    table_body = response.text.split("<tbody>")[1]
    assert "item.dispatch_failed" in table_body
    assert "item.stored" not in table_body


def test_audit_log_event_type_and_source_render_as_selects(client, conn):
    conn.execute(
        "INSERT INTO audit_log (event_type, actor, source) "
        "VALUES ('item.dispatch_failed', 'test', 'SENAPRED')"
    )
    conn.commit()

    response = client.get("/audit")

    assert response.status_code == 200
    assert '<select name="event_type"' in response.text
    assert '<select name="source"' in response.text
    assert '<option value="item.dispatch_failed"' in response.text
    assert '<option value="SENAPRED"' in response.text


def test_audit_log_q_searches_details(client, conn):
    record_audit_event(
        conn, event_type="beacon.voice.transmit_failed", actor="beacon",
        details={"error": "timeout waiting for rig"},
    )
    conn.commit()

    response = client.get("/audit", params={"q": "timeout"})

    assert response.status_code == 200
    assert "beacon.voice.transmit_failed" in response.text
