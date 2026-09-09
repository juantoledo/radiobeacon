import json

from adapters.storage import get_adapter_instance, get_setting, set_adapter_instance, set_setting
from adapters.policy import get_policy, set_policy


def _upload(client, payload, url="/config/import-export/preview"):
    files = {"upload": ("cfg.json", json.dumps(payload), "application/json")}
    return client.post(url, files=files)


def test_import_export_tab_appears_in_nav(client):
    r = client.get("/config")

    assert 'href="/config/import-export"' in r.text


def test_landing_page_renders(client):
    r = client.get("/config/import-export")

    assert r.status_code == 200
    assert "Export" in r.text
    assert "Import" in r.text


def test_download_excludes_secrets_and_is_an_attachment(client, conn):
    set_setting(conn, "ANTHROPIC_API_KEY", "sk-should-not-leak", is_secret=True)
    set_setting(conn, "DISPATCHER_INTERVAL_SECONDS", "9")

    r = client.get("/config/import-export/download")

    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert "ANTHROPIC_API_KEY" not in data["settings"]
    assert data["settings"]["DISPATCHER_INTERVAL_SECONDS"] == "9"
    assert data["schema_version"] == 2


def test_preview_of_plain_settings_file_shows_summary_and_no_custom_banner(client):
    payload = {"schema_version": 2, "settings": {"DISPATCHER_INTERVAL_SECONDS": "11"}}

    r = _upload(client, payload)

    assert r.status_code == 200
    assert "config_json" in r.text
    assert 'name="confirm_overwrite"' in r.text
    assert 'name="confirm_custom_code"' not in r.text
    assert "no sandboxing" not in r.text


def test_apply_without_overwrite_confirmation_is_rejected_and_writes_nothing(client, conn):
    payload = {"schema_version": 2, "settings": {"DISPATCHER_INTERVAL_SECONDS": "13"}}

    r = client.post("/config/import-export/apply", data={"config_json": json.dumps(payload)})

    assert r.status_code == 400
    assert get_setting("DISPATCHER_INTERVAL_SECONDS", conn=conn, env_fallback=False) is None


def test_apply_with_confirmation_upserts_and_renders_result(client, conn):
    payload = {"schema_version": 2, "settings": {"DISPATCHER_INTERVAL_SECONDS": "17"}}

    r = client.post(
        "/config/import-export/apply",
        data={"config_json": json.dumps(payload), "confirm_overwrite": "on"},
    )

    assert r.status_code == 200
    assert "Import complete" in r.text
    assert get_setting("DISPATCHER_INTERVAL_SECONDS", conn=conn, env_fallback=False) == "17"


def test_import_upserts_without_deleting_an_untouched_extra_policy(client, conn):
    set_policy(conn, "urgent", transmit_kind="interval", transmit_count=5, transmit_interval_seconds=60, description="pre-existing")
    payload = {
        "schema_version": 2,
        "policies": [{"name": "default", "fetch_kind": "interval", "transmit_kind": "once", "transmit_count": 1}],
    }

    r = client.post(
        "/config/import-export/apply",
        data={"config_json": json.dumps(payload), "confirm_overwrite": "on"},
    )

    assert r.status_code == 200
    assert get_policy(conn, "urgent") is not None  # untouched, not deleted


def test_custom_record_skipped_and_flagged_when_dev_tools_off(client, conn):
    set_setting(conn, "UI_DEV_TOOLS_ENABLED", "false")
    payload = {
        "schema_version": 2,
        "adapter_instances": [
            {"source": "sneaky", "adapter_type": "custom", "config": {"code": "def fetch(config): return []"}}
        ],
    }

    r = _upload(client, payload)

    assert r.status_code == 200
    assert "will be" in r.text and "skipped" in r.text
    assert 'name="confirm_custom_code"' not in r.text

    r2 = client.post(
        "/config/import-export/apply",
        data={"config_json": json.dumps(payload)},
    )
    assert r2.status_code == 200  # nothing needed confirming -- the only record was skipped
    assert get_adapter_instance(conn, "sneaky") is None


def test_custom_record_shown_for_review_and_requires_its_own_confirmation(client, conn):
    set_setting(conn, "UI_DEV_TOOLS_ENABLED", "true")
    payload = {
        "schema_version": 2,
        "adapter_instances": [
            {
                "source": "reviewed",
                "adapter_type": "custom",
                "config": {"code": "def fetch(config):\n    return []\n"},
            }
        ],
    }

    r = _upload(client, payload)
    assert r.status_code == 200
    assert "def fetch(config):" in r.text
    assert 'name="confirm_custom_code"' in r.text

    # overwrite confirmed but not the code-review box -> rejected, nothing written
    r2 = client.post(
        "/config/import-export/apply",
        data={"config_json": json.dumps(payload), "confirm_overwrite": "on"},
    )
    assert r2.status_code == 400
    assert get_adapter_instance(conn, "reviewed") is None

    # both boxes checked -> accepted
    r3 = client.post(
        "/config/import-export/apply",
        data={
            "config_json": json.dumps(payload),
            "confirm_overwrite": "on",
            "confirm_custom_code": "on",
        },
    )
    assert r3.status_code == 200
    stored = get_adapter_instance(conn, "reviewed")
    assert stored is not None
    assert stored["adapter_type"] == "custom"


def test_malformed_upload_redirects_with_error_instead_of_500(client):
    files = {"upload": ("cfg.json", b"not json at all", "application/json")}

    r = client.post("/config/import-export/preview", files=files, follow_redirects=False)

    assert r.status_code == 303
    assert "error" in r.headers["location"]


def test_schema_version_too_new_is_rejected_cleanly(client):
    payload = {"schema_version": 999, "settings": {}}

    r = client.post(
        "/config/import-export/preview",
        files={"upload": ("cfg.json", json.dumps(payload), "application/json")},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert "error" in r.headers["location"]
