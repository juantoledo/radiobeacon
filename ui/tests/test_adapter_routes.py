import json
from unittest.mock import patch

from adapters.storage import get_adapter_instance, get_source_fields, set_adapter_instance


def test_adapters_list_returns_200_and_shows_seeded_instances(client):
    response = client.get("/config/adapters")

    assert response.status_code == 200
    assert "csn" in response.text
    assert "senapred" in response.text


def test_nav_shows_adapters_link(client):
    # Adapters moved under Config — the link now lives in the /config sub-nav.
    response = client.get("/config")

    assert 'href="/config/adapters"' in response.text


def test_legacy_adapters_url_redirects_under_config(client):
    response = client.get("/adapters", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/config/adapters"


def test_adapter_new_page_returns_200(client):
    response = client.get("/config/adapters/new")

    assert response.status_code == 200


def test_adapter_edit_page_returns_200_for_known_source(client):
    response = client.get("/config/adapters/csn/edit")

    assert response.status_code == 200
    # CSN's seeded config prefills the structured fields, not raw JSON.
    assert 'value="https://api.gael.cloud/general/public/sismos"' in response.text
    assert 'name="map_title_template" value="Sismo M{Magnitud} - {RefGeografica}"' in response.text


def test_adapter_edit_page_404s_for_unknown_source(client):
    response = client.get("/config/adapters/does-not-exist/edit")

    assert response.status_code == 404


def test_adapter_create_persists_structured_api_config(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-source",
            "display_name": "New Source",
            "site_url": "https://example.test/",
            "adapter_type": "api",
            "enabled": "on",
            "interval_seconds": "30",
            "url": "https://example.test/api",
            "method": "GET",
            "items_path": "",
            "header_key": ["Accept"],
            "header_value": ["application/json"],
            "map_id_template": "{Id}",
            "map_title_template": "{Name}",
            "transmit_policy": "urgent",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/config/adapters?msg=")

    row = get_adapter_instance(conn, "new-source")
    assert row is not None
    assert row["adapter_type"] == "api"
    assert row["enabled"] == 1
    assert row["interval_seconds"] == 30
    config = json.loads(row["config"])
    assert config["url"] == "https://example.test/api"
    assert config["headers"] == {"Accept": "application/json"}
    assert config["mapping"]["id"] == {"template": "{Id}"}
    assert config["mapping"]["title"] == {"template": "{Name}"}
    assert "contents" not in config["mapping"]  # left blank -> omitted entirely
    assert config["transmit_policy"] == "urgent"
    assert get_source_fields(conn, "new-source") == {
        "source_name": "New Source", "source_url": "https://example.test/",
    }


def test_adapter_create_custom_type_config_is_only_code(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-custom",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = get_adapter_instance(conn, "new-custom")
    config = json.loads(row["config"])
    assert set(config.keys()) == {"code"}
    assert config["code"] == "def fetch(config):\n    return []\n"


def test_adapter_create_rejects_missing_url(client, conn):
    response = client.post(
        "/config/adapters",
        data={"mode": "create", "source": "broken", "adapter_type": "api", "url": ""},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert get_adapter_instance(conn, "broken") is None


def test_adapter_create_api_transmit_policy_defaults_are_omitted(client, conn):
    """The default policy name resolves from a NULL items.transmit_policy
    anyway, so it's left out of the stored blob."""
    client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "plain",
            "adapter_type": "api",
            "url": "https://example.test/",
            "map_id_template": "{Id}",
            "transmit_policy": "informational",
        },
        follow_redirects=False,
    )

    config = json.loads(get_adapter_instance(conn, "plain")["config"])
    assert "transmit_policy" not in config


def test_adapter_edit_updates_existing_instance(client, conn):
    response = client.post(
        "/config/adapters/csn",
        data={
            "mode": "edit",
            "source": "csn",
            "adapter_type": "api",
            "enabled": "on",
            "url": "https://edited.example/",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = get_adapter_instance(conn, "csn")
    config = json.loads(row["config"])
    assert config["url"] == "https://edited.example/"
    assert config["mapping"] == {}  # not carried over — only what was posted


def test_adapter_delete_removes_row_and_redirects(client, conn):
    response = client.post("/config/adapters/csn/delete", follow_redirects=False)

    assert response.status_code == 303
    assert get_adapter_instance(conn, "csn") is None


def test_adapter_delete_unknown_source_still_redirects(client):
    response = client.post("/config/adapters/does-not-exist/delete", follow_redirects=False)

    assert response.status_code == 303


def test_adapter_test_action_runs_fetch_and_shows_result(client):
    response = client.post(
        "/config/adapters/test",
        data={
            "mode": "create",
            "source": "fake-test-source",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return [{'id': '1', 'title': 'X'}]\n",
        },
    )

    assert response.status_code == 200
    assert "Fetched 1 item" in response.text


def test_adapter_test_action_shows_every_mapped_field(client):
    response = client.post(
        "/config/adapters/test",
        data={
            "mode": "create",
            "source": "fake-test-source",
            "adapter_type": "custom",
            "code": (
                "def fetch(config):\n"
                "    return [{\n"
                "        'id': '1', 'title': 'T', 'contents': 'C', 'url': 'https://example.test/',\n"
                "        'event_key': 'E', 'type': 'Alerta', 'subtype': 'Viento',\n"
                "        'transmit_policy': 'urgent', 'raw': {'Original': 'Field'},\n"
                "    }]\n"
            ),
        },
    )

    assert response.status_code == 200
    assert "Fetched 1 item" in response.text
    for value in ("T", "C", "https://example.test/", "E", "Alerta", "Viento", "urgent"):
        assert value in response.text
    # `raw` renders as syntax-highlighted, collapsible JSON, not a column.
    assert "<summary" in response.text
    assert 'data-field-name="Original"' not in response.text  # non-interactive here
    assert '<span class="json-key">"Original"</span>' in response.text


def test_adapter_test_action_shows_error_on_failure(client):
    response = client.post(
        "/config/adapters/test",
        data={
            "mode": "create",
            "source": "fake-test-source",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    raise RuntimeError('boom')\n",
        },
    )

    assert response.status_code == 200
    assert "boom" in response.text


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_adapter_test_action_for_api_type_uses_structured_fields(client):
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse([{"Fecha": "2026-01-01T00:00:00"}]),
    ):
        response = client.post(
            "/config/adapters/test",
            data={
                "mode": "create",
                "source": "fake-api-source",
                "adapter_type": "api",
                "url": "https://api.example.test/",
                "method": "GET",
                "map_id_template": "{Fecha}",
            },
        )

    assert response.status_code == 200
    assert "Fetched 1 item" in response.text


def test_adapter_test_action_does_not_persist_anything(client, conn):
    client.post(
        "/config/adapters/test",
        data={
            "mode": "create",
            "source": "not-saved",
            "adapter_type": "api",
            "url": "https://example.test/",
        },
    )

    assert get_adapter_instance(conn, "not-saved") is None


def test_adapter_sample_action_shows_raw_unmapped_response(client):
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse([{"Fecha": "2026-01-01 00:00:00", "Magnitud": "3.0"}]),
    ):
        response = client.post(
            "/config/adapters/sample",
            data={
                "mode": "create",
                "source": "csn",
                "adapter_type": "api",
                "url": "https://api.example.test/",
                "method": "GET",
            },
        )

    assert response.status_code == 200
    assert "Showing 1 of 1 item" in response.text
    assert "Fecha" in response.text
    assert "2026-01-01 00:00:00" in response.text
    # The discovered field names populate the mapping inputs' datalist and
    # render as clickable chips.
    assert '<option value="Fecha">' in response.text
    assert '<option value="Magnitud">' in response.text
    assert 'data-field-name="Fecha"' in response.text
    # Root array detected as a candidate list — blank items_path is
    # already correct, so this is offered as "(root)".
    assert '(root)' in response.text
    assert 'data-path=""' in response.text
    assert "resolves to a list of 1 item" in response.text


def test_adapter_sample_action_json_preview_top_level_keys_are_draggable(client):
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse(
            [{"Fecha": "2026-01-01 00:00:00", "variableRiesgo": {"nombre": "Viento"}}]
        ),
    ):
        response = client.post(
            "/config/adapters/sample",
            data={
                "mode": "create",
                "source": "csn",
                "adapter_type": "api",
                "url": "https://api.example.test/",
            },
        )

    assert response.status_code == 200
    # Top-level item keys are draggable/clickable — FieldMapping.field can
    # actually resolve these via a flat item.get(name).
    assert 'class="json-key draggable-field" draggable="true" data-field-name="Fecha"' in response.text
    # Nested keys (inside variableRiesgo) render as plain JSON — dragging
    # "nombre" onto a mapping row would set field="nombre", which
    # item.get("nombre") could never resolve (it's nested), so offering
    # it as draggable would be actively misleading.
    assert 'data-field-name="nombre"' not in response.text
    assert '<span class="json-key">"nombre"</span>' in response.text


def test_adapter_sample_action_escapes_untrusted_response_content(client):
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse([{"<script>alert(1)</script>": "<img src=x onerror=alert(1)>"}]),
    ):
        response = client.post(
            "/config/adapters/sample",
            data={
                "mode": "create",
                "source": "csn",
                "adapter_type": "api",
                "url": "https://api.example.test/",
            },
        )

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "<img src=x onerror=alert(1)>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_adapter_sample_action_needs_no_mapping_configured(client):
    """No map_*/date_field/dispatch_* fields posted at all — only
    connection settings — must still work, since the whole point is
    previewing a response before any mapping is configured."""
    with patch("urllib.request.urlopen", return_value=_FakeResponse([{"a": 1}])):
        response = client.post(
            "/config/adapters/sample",
            data={
                "mode": "create",
                "source": "new-source",
                "adapter_type": "api",
                "url": "https://api.example.test/",
            },
        )

    assert response.status_code == 200
    assert "Showing 1 of 1 item" in response.text


def test_adapter_sample_action_detects_nested_array_and_warns_when_items_path_is_blank(client):
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse({"result": {"items": [{"Id": "1"}, {"Id": "2"}]}}),
    ):
        response = client.post(
            "/config/adapters/sample",
            data={
                "mode": "create",
                "source": "csn",
                "adapter_type": "api",
                "url": "https://api.example.test/",
                "items_path": "",
            },
        )

    assert response.status_code == 200
    # items_path="" resolves to the whole dict, not a list — warn rather
    # than silently treating the entire response as "one item".
    assert "not a list" in response.text
    # ...but the real list one level down is offered as a one-click fix.
    assert 'data-path="result.items"' in response.text
    assert "result.items — 2 items" in response.text


def test_adapter_sample_action_shows_error_on_failure(client):
    import urllib.error

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        response = client.post(
            "/config/adapters/sample",
            data={
                "mode": "create",
                "source": "csn",
                "adapter_type": "api",
                "url": "https://api.example.test/",
            },
        )

    assert response.status_code == 200
    assert "Sample fetch failed" in response.text


def test_adapter_sample_action_rejects_custom_type(client):
    response = client.post(
        "/config/adapters/sample",
        data={
            "mode": "create",
            "source": "senapred",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
        },
    )

    assert response.status_code == 200
    assert "only available for API-type adapters" in response.text


def test_adapter_sample_action_does_not_persist_anything(client, conn):
    with patch("urllib.request.urlopen", return_value=_FakeResponse([{"a": 1}])):
        client.post(
            "/config/adapters/sample",
            data={
                "mode": "create",
                "source": "not-saved",
                "adapter_type": "api",
                "url": "https://api.example.test/",
            },
        )

    assert get_adapter_instance(conn, "not-saved") is None


def test_adapter_create_persists_ai_prompt_override(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-custom",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
            "ai_prompt": "Resume distinto {extracted_contents}",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    config = json.loads(get_adapter_instance(conn, "new-custom")["config"])
    assert config["ai_prompt"] == "Resume distinto {extracted_contents}"


def test_adapter_create_omits_blank_ai_prompt(client, conn):
    client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-custom",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
            "ai_prompt": "   ",
        },
        follow_redirects=False,
    )

    config = json.loads(get_adapter_instance(conn, "new-custom")["config"])
    assert "ai_prompt" not in config


def test_adapter_form_shows_default_prompt_as_placeholder_when_not_overridden(client, conn):
    from adapters.actions_defaults import AI_PROMPT_DEFAULT

    response = client.get("/config/adapters/csn/edit")

    assert response.status_code == 200
    # built-in default shown greyed (placeholder), not as the field value
    assert 'placeholder="Resume el siguiente aviso' in response.text
    assert "{extracted_contents}" in AI_PROMPT_DEFAULT


def test_adapter_form_placeholder_reflects_global_override(client, conn):
    from adapters.storage import set_setting

    set_setting(conn, "ACTIONS_AI_PROMPT", "GLOBAL DEFAULT {extracted_contents}")

    response = client.get("/config/adapters/csn/edit")

    assert 'placeholder="GLOBAL DEFAULT {extracted_contents}"' in response.text


def test_adapter_edit_page_prefills_ai_prompt(client, conn):
    set_adapter_instance(
        conn,
        "csn",
        "custom",
        {"code": "def fetch(config): return []", "ai_prompt": "MARKER-PREFILL {url}"},
    )

    response = client.get("/config/adapters/csn/edit")

    assert response.status_code == 200
    assert "MARKER-PREFILL {url}" in response.text


def test_adapter_create_persists_ai_fallback_to_title(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-custom",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
            "ai_fallback_to_title": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    config = json.loads(get_adapter_instance(conn, "new-custom")["config"])
    assert config["ai_fallback_to_title"] is True


def test_adapter_create_omits_ai_fallback_to_title_when_unchecked(client, conn):
    client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-custom",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
        },
        follow_redirects=False,
    )

    config = json.loads(get_adapter_instance(conn, "new-custom")["config"])
    assert "ai_fallback_to_title" not in config


def test_adapter_edit_page_checks_ai_fallback_to_title_box_when_set(client, conn):
    set_adapter_instance(
        conn,
        "csn",
        "custom",
        {"code": "def fetch(config): return []", "ai_fallback_to_title": True},
    )

    response = client.get("/config/adapters/csn/edit")

    assert response.status_code == 200
    assert 'id="ai_fallback_to_title"' in response.text
    checkbox = response.text.split('id="ai_fallback_to_title"', 1)[1].split(">", 1)[0]
    assert "checked" in checkbox


def test_adapter_new_page_offers_aiprompt_type(client):
    response = client.get("/config/adapters/new")

    assert response.status_code == 200
    assert 'value="aiprompt"' in response.text
    assert 'id="aiprompt-fields"' in response.text


def test_adapter_create_persists_aiprompt_config(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "wx",
            "adapter_type": "aiprompt",
            "aip_prompt": "Weather report for {date}.",
            "aip_cron": "0 6 * * *",
            "aip_transmit_policy": "informational",
            "aip_title_template": "Weather — {date}",
            "aip_type": "weather",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    config = json.loads(get_adapter_instance(conn, "wx")["config"])
    # transmit_policy == the default -> omitted from the stored blob
    assert config == {
        "prompt": "Weather report for {date}.",
        "cron": "0 6 * * *",
        "title_template": "Weather — {date}",
        "type": "weather",
    }


def test_adapter_create_aiprompt_requires_prompt(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "broken-ai",
            "adapter_type": "aiprompt",
            "aip_prompt": "   ",
            "aip_cron": "0 6 * * *",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Prompt is required." in response.text
    assert get_adapter_instance(conn, "broken-ai") is None


def test_adapter_create_aiprompt_rejects_bad_cron(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "broken-ai",
            "adapter_type": "aiprompt",
            "aip_prompt": "Weather report.",
            "aip_cron": "not a cron",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "cron" in response.text.lower()
    assert get_adapter_instance(conn, "broken-ai") is None


def test_adapter_edit_page_prefills_aiprompt_fields(client, conn):
    set_adapter_instance(
        conn,
        "wx",
        "aiprompt",
        {"prompt": "MARKER-PROMPT {date}", "cron": "0 6 * * *", "type": "weather"},
    )

    response = client.get("/config/adapters/wx/edit")

    assert response.status_code == 200
    assert "MARKER-PROMPT {date}" in response.text
    assert 'value="0 6 * * *"' in response.text
    assert 'value="weather"' in response.text
