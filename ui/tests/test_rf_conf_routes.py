"""The Beacon — SvxLink / Beacon — Direwolf config pages and their optional
raw conf-file editor (ui.routers.rf_conf)."""
from urllib.parse import unquote_plus

from adapters.storage import get_setting, set_setting


def test_svxlink_page_renders_catalog_fields(client):
    response = client.get("/config/beacon-svxlink")

    assert response.status_code == 200
    assert "BEACON_WAV_TRANSMITTER" in response.text
    assert "BEACON_TXQUEUE_INCOMING_DIR" in response.text
    assert "BEACON_SVXLINK_CONF_PATH" in response.text
    assert "BEACON_RF_CONF_EDITOR_ENABLED" in response.text


def test_direwolf_page_renders_catalog_fields(client):
    response = client.get("/config/beacon-direwolf")

    assert response.status_code == 200
    assert "BEACON_GEN_PACKETS_BINARY" in response.text
    assert "BEACON_FRAME_LEAD_SILENCE_MS" in response.text
    assert "BEACON_DIREWOLF_CONF_PATH" in response.text


def test_catalog_save_still_goes_through_the_generic_handler(client, conn):
    response = client.post(
        "/config/beacon-svxlink",
        data={"BEACON_WAV_TRANSMITTER": "spool"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/config/beacon-svxlink?msg=")
    assert get_setting("BEACON_WAV_TRANSMITTER", conn=conn) == "spool"


def test_editor_hidden_and_post_404s_while_flag_off(client):
    page = client.get("/config/beacon-svxlink")
    assert '<textarea class="sql-box" name="content"' not in page.text

    post = client.post(
        "/config/beacon-svxlink/conf", data={"content": "x"}, follow_redirects=False
    )
    assert post.status_code == 404


def test_editor_shows_file_contents_when_enabled(client, conn, tmp_path):
    conf = tmp_path / "svxlink.conf"
    conf.write_text("[SimplexLogic]\nTYPE=Simplex\n")
    set_setting(conn, "BEACON_RF_CONF_EDITOR_ENABLED", "true")
    set_setting(conn, "BEACON_SVXLINK_CONF_PATH", str(conf))

    response = client.get("/config/beacon-svxlink")

    assert response.status_code == 200
    assert '<textarea class="sql-box" name="content"' in response.text
    assert "TYPE=Simplex" in response.text


def test_editor_save_writes_file_backup_and_audit_row(client, conn, tmp_path):
    conf = tmp_path / "svxlink.conf"
    conf.write_text("[SimplexLogic]\nTYPE=Simplex\n")
    set_setting(conn, "BEACON_RF_CONF_EDITOR_ENABLED", "true")
    set_setting(conn, "BEACON_SVXLINK_CONF_PATH", str(conf))

    new_body = "[SimplexLogic]\nTYPE=Simplex\nCOMMAND_PTY=/dev/shm/svxlink_simplex_ctrl"
    response = client.post(
        "/config/beacon-svxlink/conf",
        data={"content": new_body},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "systemctl restart svxlink" in unquote_plus(response.headers["location"])
    assert conf.read_text() == new_body + "\n"  # trailing newline normalized
    backups = list(tmp_path.glob("svxlink.conf.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == "[SimplexLogic]\nTYPE=Simplex\n"

    audit_rows = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.rf_conf.edited'"
    ).fetchall()
    assert len(audit_rows) == 1
    (details,) = audit_rows[0]
    assert "COMMAND_PTY" not in (details or "")  # never the file body
    assert "svxlink.conf" in (details or "")


def test_editor_creates_file_when_absent(client, conn, tmp_path):
    conf = tmp_path / "new.conf"
    set_setting(conn, "BEACON_RF_CONF_EDITOR_ENABLED", "true")
    set_setting(conn, "BEACON_SVXLINK_CONF_PATH", str(conf))

    page = client.get("/config/beacon-svxlink")
    assert page.status_code == 200
    assert "does not exist yet" in page.text

    client.post("/config/beacon-svxlink/conf", data={"content": "PTT GPIO 25"}, follow_redirects=False)
    assert conf.read_text() == "PTT GPIO 25\n"
    assert not list(tmp_path.glob("new.conf.*.bak"))  # nothing to back up


def test_editor_missing_parent_dir_is_a_friendly_message(client, conn):
    set_setting(conn, "BEACON_RF_CONF_EDITOR_ENABLED", "true")
    set_setting(conn, "BEACON_SVXLINK_CONF_PATH", "/no/such/dir/svxlink.conf")

    response = client.get("/config/beacon-svxlink")

    assert response.status_code == 200
    assert "does not exist" in response.text
    assert '<textarea class="sql-box" name="content"' not in response.text


def test_direwolf_editor_hidden_when_path_blank_even_if_enabled(client, conn):
    set_setting(conn, "BEACON_RF_CONF_EDITOR_ENABLED", "true")

    response = client.get("/config/beacon-direwolf")

    assert response.status_code == 200
    assert "No file path configured" in response.text


def test_config_nav_still_lists_every_tab_from_the_rf_pages(client):
    response = client.get("/config/beacon-svxlink")

    for href in ("/config/adapters", "/config/beacon", "/config/policies"):
        assert f'href="{href}"' in response.text
