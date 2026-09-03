from adapters.storage import get_setting, list_settings, set_setting

from ui.routers.config import SECRET_SENTINEL


def test_config_root_redirects_to_adapters_tab(client):
    response = client.get("/config", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/config/adapters"


def test_mq_category_tab_lists_every_group_regardless_of_advanced(client):
    response = client.get("/config/mq")

    assert response.status_code == 200
    assert "MQ — Dispatcher" in response.text
    assert "MQ — Actions" in response.text
    assert "DISPATCHER_MQ_HOST" in response.text  # advanced, still shown on its own tab


def test_actions_category_tab_lists_basic_and_wiring_keys_together(client):
    response = client.get("/config/actions")

    assert response.status_code == 200
    assert "ACTIONS_AI_PROVIDER" in response.text  # basic
    assert "ACTIONS_AI_OUTPUT_TOPIC" in response.text  # wiring, no longer hidden on a separate tab


def test_beacon_category_tab_lists_every_beacon_group(client):
    response = client.get("/config/beacon")

    assert response.status_code == 200
    assert "Beacon — Identity" in response.text
    assert "Beacon — MQ" in response.text  # advanced
    assert "BEACON_MQ_HOST" in response.text


def test_config_nav_links_to_every_tab(client):
    response = client.get("/config/beacon")

    for href in (
        "/config/adapters",
        "/config/dispatcher",
        "/config/mq",
        "/config/actions",
        "/config/beacon",
        "/config/secrets",
        "/config/display",
        "/config/ui",
        "/config/policies",
    ):
        assert f'href="{href}"' in response.text


def test_single_group_category_tab_is_the_group_edit_form(client):
    response = client.get("/config/dispatcher")

    assert response.status_code == 200
    assert "DISPATCHER_INTERVAL_SECONDS" in response.text
    assert "5" in response.text  # catalog default shown when no override exists


def test_config_group_edit_page_404s_for_unknown_group(client):
    response = client.get("/config/not-a-real-group")

    assert response.status_code == 404


def test_config_group_save_persists_and_redirects(client, conn):
    response = client.post(
        "/config/dispatcher",
        data={"DISPATCHER_INTERVAL_SECONDS": "15"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/config/dispatcher?msg=")
    assert get_setting("DISPATCHER_INTERVAL_SECONDS", conn=conn) == "15"


def test_config_group_save_ignores_blank_fields(client, conn):
    """A blank field means "no explicit value", not "override to empty
    string" — get_setting() treats a stored "" as a real override (distinct
    from no row at all), so writing "" here would force every unfilled
    field to resolve to "" instead of falling through to its env var or
    catalog default."""
    client.post(
        "/config/dispatcher",
        data={
            "DISPATCHER_INTERVAL_SECONDS": "15",
            "DISPATCHER_CONSUMER_NAME": "",
        },
        follow_redirects=False,
    )

    rows = list_settings(conn)
    assert [row[0] for row in rows] == ["DISPATCHER_INTERVAL_SECONDS"]


def test_config_group_edit_page_shows_env_value_when_no_db_override(client, monkeypatch):
    monkeypatch.setenv("DISPATCHER_CONSUMER_NAME", "custom-consumer")

    response = client.get("/config/dispatcher")

    assert "custom-consumer" in response.text


def test_config_setting_reset_deletes_override_and_redirects(client, conn):
    set_setting(conn, "DISPATCHER_INTERVAL_SECONDS", "15")

    response = client.post(
        "/config/dispatcher/DISPATCHER_INTERVAL_SECONDS/reset",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_setting("DISPATCHER_INTERVAL_SECONDS", conn=conn) is None


def test_config_setting_reset_404s_for_key_not_in_group(client):
    response = client.post(
        "/config/dispatcher/ANTHROPIC_API_KEY/reset", follow_redirects=False
    )

    assert response.status_code == 404


def test_nav_shows_config_link(client):
    response = client.get("/")

    assert 'href="/config"' in response.text


def test_adapters_tab_shows_general_setting_and_instance_list(client):
    response = client.get("/config/adapters")

    assert response.status_code == 200
    assert "ADAPTERS_DEFAULT_INTERVAL_SECONDS" in response.text
    assert 'href="/config/adapters-general"' in response.text
    assert 'href="/config/adapters/new"' in response.text


def test_adapters_general_group_edit_back_url_points_at_adapters_tab(client):
    response = client.get("/config/adapters-general")

    assert response.status_code == 200
    assert 'href="/config/adapters"' in response.text


# --- secret masking: the single most important test in this feature ---


def test_secret_round_trip_never_leaks_plaintext(client, conn):
    real_value = "sk-ant-super-secret-do-not-leak"

    # Set it for the first time.
    save = client.post(
        "/config/secrets",
        data={"ANTHROPIC_API_KEY": real_value, "OPENAI_API_KEY": ""},
        follow_redirects=False,
    )
    assert save.status_code == 303
    assert get_setting("ANTHROPIC_API_KEY", conn=conn) == real_value

    # GET must show the masked sentinel, never the real value.
    page = client.get("/config/secrets")
    assert real_value not in page.text
    assert SECRET_SENTINEL in page.text

    # The MQ category tab (a different config page) must not leak it either.
    other_page = client.get("/config/mq")
    assert real_value not in other_page.text

    # Re-submitting the sentinel (what the browser would send back
    # unmodified) must NOT overwrite the stored secret.
    resave = client.post(
        "/config/secrets",
        data={"ANTHROPIC_API_KEY": SECRET_SENTINEL, "OPENAI_API_KEY": ""},
        follow_redirects=False,
    )
    assert resave.status_code == 303
    assert get_setting("ANTHROPIC_API_KEY", conn=conn) == real_value

    # A genuinely new value does overwrite it.
    new_value = "sk-ant-rotated-value"
    client.post(
        "/config/secrets",
        data={"ANTHROPIC_API_KEY": new_value, "OPENAI_API_KEY": ""},
        follow_redirects=False,
    )
    assert get_setting("ANTHROPIC_API_KEY", conn=conn) == new_value

    # The audit log must never carry the plaintext either.
    audit_rows = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'setting.changed'"
    ).fetchall()
    for (details,) in audit_rows:
        assert real_value not in (details or "")
        assert new_value not in (details or "")


def test_secret_field_empty_when_nothing_stored(client):
    response = client.get("/config/secrets")

    # No DB row yet -> field renders empty, not the sentinel, so an
    # operator can tell "nothing stored" apart from "something is stored".
    assert f'id="ANTHROPIC_API_KEY"' in response.text
    assert SECRET_SENTINEL not in response.text
