from adapters.storage import set_setting
from dispatcher.policy import set_policy

from ui import config


def _insert_item(conn, source, item_id, *, dispatch_policy="informational"):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, fetched_at, dispatch_policy, rawdata) "
        "VALUES (?, ?, 'Title', datetime('now'), ?, '{}')",
        (source, item_id, dispatch_policy),
    )
    conn.commit()


def _configure_beacon(conn):
    """Satisfies is_beacon_configured() so override/rearm actions aren't
    blocked — see ui.beacon.BEACON_FIELDS for the full required set."""
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


def test_dashboard_includes_auto_refresh_script_when_enabled(client, monkeypatch):
    monkeypatch.setattr(config, "UI_DASHBOARD_REFRESH_SECONDS", 5)

    response = client.get("/")

    assert 'id="dashboard-content"' in response.text
    assert "dashboard-refresh.js" in response.text
    assert 'data-interval-ms="5000"' in response.text


def test_dashboard_omits_auto_refresh_script_when_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "UI_DASHBOARD_REFRESH_SECONDS", 0)

    response = client.get("/")

    assert "dashboard-refresh.js" not in response.text


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
    _insert_item(conn, "csn", "1", dispatch_policy="informational")

    response = client.post(
        "/items/csn/1/override", data={"dispatch_policy": "urgent"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/items/csn/1")
    row = conn.execute(
        "SELECT dispatch_policy FROM items WHERE source='csn' AND item_id='1'"
    ).fetchone()
    assert row[0] == "urgent"


def test_override_action_missing_dispatch_policy_is_rejected(client, conn):
    _configure_beacon(conn)
    _insert_item(conn, "csn", "1")

    response = client.post("/items/csn/1/override", data={}, follow_redirects=False)

    assert response.status_code == 422


def test_override_action_blocked_when_beacon_not_configured(client, conn):
    _insert_item(conn, "csn", "1", dispatch_policy="informational")

    response = client.post(
        "/items/csn/1/override", data={"dispatch_policy": "urgent"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    row = conn.execute(
        "SELECT dispatch_policy FROM items WHERE source='csn' AND item_id='1'"
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


def test_policies_list_returns_200(client):
    response = client.get("/policies")
    assert response.status_code == 200
    # seeded by _ensure_tables
    assert "urgent" in response.text
    assert "informational" in response.text


def test_policy_create_redirects_and_persists(client, conn):
    response = client.post(
        "/policies",
        data={
            "name": "critical",
            "repeat_times": "10",
            "interval_seconds": "30",
            "description": "test policy",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/policies")
    row = conn.execute(
        "SELECT repeat_times, interval_seconds FROM dispatch_policies WHERE name='critical'"
    ).fetchone()
    assert tuple(row) == (10, 30)


def test_policy_edit_page_returns_200_for_known_policy(client, conn):
    set_policy(conn, "custom", 2, 15, "desc")

    response = client.get("/policies/custom/edit")

    assert response.status_code == 200
    assert "custom" in response.text


def test_policy_edit_page_returns_404_for_unknown_policy(client):
    response = client.get("/policies/does-not-exist/edit")
    assert response.status_code == 404


def test_policy_delete_redirects(client, conn):
    set_policy(conn, "temp", 1, 0)

    response = client.post("/policies/temp/delete", follow_redirects=False)

    assert response.status_code == 303
    row = conn.execute("SELECT 1 FROM dispatch_policies WHERE name='temp'").fetchone()
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
