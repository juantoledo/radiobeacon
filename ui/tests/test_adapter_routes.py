import json
from unittest.mock import patch

from adapters.storage import (
    get_adapter_instance,
    get_source_fields,
    set_adapter_instance,
    set_setting,
)


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


def test_adapter_new_page_renders_contents_mapping_as_a_textarea(client):
    # contents is the one mapping row long/nested enough to warrant a
    # textarea instead of the shared one-line <input> the other 6 fields use.
    response = client.get("/config/adapters/new")

    assert response.status_code == 200
    assert '<textarea name="map_contents_template"' in response.text
    assert '<input type="text" name="map_id_template"' in response.text
    assert '<input type="text" name="map_contents_template"' not in response.text


def test_adapter_new_page_renders_type_picker_pills(client):
    # adapter_type is a hidden input driven by 3 segmented pills, not a
    # <select> — see adapter_form.html's redesign.
    response = client.get("/config/adapters/new")

    assert response.status_code == 200
    assert 'id="adapter_type" name="adapter_type" value="api"' in response.text
    assert 'data-adapter-type="api"' in response.text
    assert 'data-adapter-type="custom"' in response.text
    assert 'data-adapter-type="aiprompt"' in response.text


def test_adapter_new_page_advanced_sections_start_collapsed(client):
    # A fresh adapter has no headers/query params/date parsing configured
    # yet — those <details> should start closed, not clutter the page.
    response = client.get("/config/adapters/new")

    assert response.status_code == 200
    assert '<details class="config-section" open>' not in response.text
    assert response.text.count('<details class="config-section" >') >= 3  # headers, params, date parsing


def test_adapter_edit_page_expands_populated_headers_section(client, conn):
    set_adapter_instance(
        conn,
        "csn",
        "api",
        {"url": "https://example.test/", "headers": {"Accept": "application/json"}},
    )

    response = client.get("/config/adapters/csn/edit")

    assert response.status_code == 200
    assert '<details class="config-section" open>' in response.text


def test_adapter_edit_page_expands_populated_date_parsing_section(client, conn):
    set_adapter_instance(
        conn, "csn", "api", {"url": "https://example.test/", "date_field": "Fecha"}
    )

    response = client.get("/config/adapters/csn/edit")

    assert response.status_code == 200
    assert '<summary>' in response.text
    # The date-parsing <details> is the only one open on this config (no
    # headers/query_params set) — assert at least one open <details> exists.
    assert '<details class="config-section" open>' in response.text


def test_adapter_new_page_voice_replacements_section_starts_collapsed(client):
    response = client.get("/config/adapters/new")

    assert response.status_code == 200
    assert '<details class="config-section panel" >' in response.text


def test_adapter_edit_page_expands_populated_voice_replacements_section(client, conn):
    set_adapter_instance(
        conn,
        "csn",
        "api",
        {"url": "https://example.test/", "voice_replacements": {"M": "magnitud"}},
    )

    response = client.get("/config/adapters/csn/edit")

    assert response.status_code == 200
    assert '<details class="config-section panel" open>' in response.text


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
            "url": "https://example.test/api",
            "method": "GET",
            "items_path": "",
            "header_key": ["Accept"],
            "header_value": ["application/json"],
            "map_id_template": "{Id}",
            "map_title_template": "{Name}",
            "policy": "urgent",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/config/adapters?msg=")

    row = get_adapter_instance(conn, "new-source")
    assert row is not None
    assert row["adapter_type"] == "api"
    assert row["enabled"] == 1
    assert row["policy"] == "urgent"
    config = json.loads(row["config"])
    assert config["url"] == "https://example.test/api"
    assert config["response_format"] == "json"  # left unset in the post -> defaults
    assert config["headers"] == {"Accept": "application/json"}
    assert config["mapping"]["id"] == {"template": "{Id}"}
    assert config["mapping"]["title"] == {"template": "{Name}"}
    assert "contents" not in config["mapping"]  # left blank -> omitted entirely
    assert get_source_fields(conn, "new-source") == {
        "source_name": "New Source", "source_url": "https://example.test/",
    }


def test_adapter_create_persists_xml_response_format(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "xml-source",
            "adapter_type": "api",
            "url": "https://example.test/feed.xml",
            "method": "GET",
            "response_format": "xml",
            "items_path": "items.item",
            "map_id_template": "{@id}",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = get_adapter_instance(conn, "xml-source")
    config = json.loads(row["config"])
    assert config["response_format"] == "xml"


def test_adapter_new_page_offers_response_format_select(client):
    response = client.get("/config/adapters/new")

    assert response.status_code == 200
    assert 'id="response_format"' in response.text
    assert 'value="json" selected' in response.text
    assert 'value="xml"' in response.text


def test_adapter_edit_page_prefills_saved_response_format(client, conn):
    set_adapter_instance(
        conn, "xml-edit", "api",
        {"url": "https://x", "response_format": "xml", "items_path": "items.item"},
    )

    response = client.get("/config/adapters/xml-edit/edit")

    assert response.status_code == 200
    assert 'value="xml" selected' in response.text


def test_adapter_create_custom_type_config_is_only_code(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-custom",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
            "use_ai": "on",
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


def test_adapter_create_api_policy_defaults_are_omitted(client, conn):
    """The default policy name resolves from a NULL items.policy
    anyway, so it's left out of the stored blob."""
    client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "plain",
            "adapter_type": "api",
            "url": "https://example.test/",
            "map_id_template": "{Id}",
            "policy": "informational",
        },
        follow_redirects=False,
    )

    config = json.loads(get_adapter_instance(conn, "plain")["config"])
    assert "policy" not in config


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


def test_adapter_delete_ajax_returns_fragment_without_deleted_row(client, conn):
    response = client.post(
        "/config/adapters/csn/delete", headers={"X-Requested-With": "fetch"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "fragment" in body
    assert 'data-cell="adapters-rows"' in body["fragment"]
    assert ">csn<" not in body["fragment"]
    assert get_adapter_instance(conn, "csn") is None


def test_adapter_toggle_ajax_returns_fragment(client, conn):
    row = get_adapter_instance(conn, "csn")
    was_enabled = bool(row["enabled"])

    response = client.post(
        "/config/adapters/csn/toggle", headers={"X-Requested-With": "fetch"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert 'data-cell="adapters-rows"' in body["fragment"]
    assert bool(get_adapter_instance(conn, "csn")["enabled"]) is not was_enabled


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


def test_custom_adapter_is_blocked_when_dev_tools_disabled(client, conn):
    from adapters.storage import set_setting

    set_setting(conn, "UI_DEV_TOOLS_ENABLED", "false")

    save = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "sneaky",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
        },
        follow_redirects=False,
    )
    assert save.status_code == 403
    assert get_adapter_instance(conn, "sneaky") is None

    test = client.post(
        "/config/adapters/test",
        data={
            "mode": "create",
            "source": "sneaky",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
        },
    )
    assert test.status_code == 403

    # API-type adapters are unaffected by the switch.
    ok = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "plain-api",
            "adapter_type": "api",
            "url": "https://example.test/",
            "map_id_template": "{Id}",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303


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
                "        'policy': 'urgent', 'raw': {'Original': 'Field'},\n"
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

    def read(self, amt=None):
        return self._body if amt is None else self._body[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_adapter_test_action_for_api_type_uses_structured_fields(client):
    with patch(
        "adapters.api_adapter._OPENER.open",
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
        "adapters.api_adapter._OPENER.open",
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


def test_adapter_sample_action_json_preview_keys_are_draggable_at_every_depth(client):
    with patch(
        "adapters.api_adapter._OPENER.open",
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
    # resolve these via a flat item.get(name).
    assert 'class="json-key draggable-field" draggable="true" data-field-name="Fecha"' in response.text
    # Nested keys (inside variableRiesgo) are draggable too, carrying the
    # full dotted path — FieldMapping now resolves
    # "variableRiesgo.nombre" via resolve_dotted_path.
    assert (
        'class="json-key draggable-field" draggable="true" data-field-name="variableRiesgo.nombre"'
        in response.text
    )


def test_adapter_sample_action_escapes_untrusted_response_content(client):
    with patch(
        "adapters.api_adapter._OPENER.open",
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
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse([{"a": 1}])):
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
        "adapters.api_adapter._OPENER.open",
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

    with patch("adapters.api_adapter._OPENER.open", side_effect=urllib.error.URLError("boom")):
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
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse([{"a": 1}])):
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


def test_adapter_new_page_use_ai_checked_by_default(client):
    response = client.get("/config/adapters/new")

    assert response.status_code == 200
    assert '<input id="use_ai" name="use_ai" type="checkbox" checked>' in response.text


def test_adapter_create_unchecked_use_ai_persists_false(client, conn):
    client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-custom",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
            # use_ai omitted entirely — an unchecked checkbox never appears
            # in a real browser's POST body.
        },
        follow_redirects=False,
    )

    config = json.loads(get_adapter_instance(conn, "new-custom")["config"])
    assert config["use_ai"] is False


def test_adapter_create_checked_use_ai_is_omitted_as_the_default(client, conn):
    client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-custom",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
            "use_ai": "on",
        },
        follow_redirects=False,
    )

    config = json.loads(get_adapter_instance(conn, "new-custom")["config"])
    assert "use_ai" not in config


def test_adapter_edit_page_prefills_unchecked_use_ai(client, conn):
    set_adapter_instance(
        conn, "csn", "custom", {"code": "def fetch(config): return []", "use_ai": False}
    )

    response = client.get("/config/adapters/csn/edit")

    assert response.status_code == 200
    assert '<input id="use_ai" name="use_ai" type="checkbox" >' in response.text


def test_adapter_create_persists_ai_on_failure(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-custom",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
            "ai_on_failure": "abort",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    config = json.loads(get_adapter_instance(conn, "new-custom")["config"])
    assert config["ai_on_failure"] == "abort"


def test_adapter_create_omits_ai_on_failure_when_default(client, conn):
    client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "new-custom",
            "adapter_type": "custom",
            "code": "def fetch(config):\n    return []\n",
            "ai_on_failure": "continue_with_contents",
        },
        follow_redirects=False,
    )

    config = json.loads(get_adapter_instance(conn, "new-custom")["config"])
    assert "ai_on_failure" not in config


def test_adapter_save_drops_legacy_ai_fallback_to_title(client, conn):
    set_adapter_instance(
        conn, "csn", "custom",
        {"code": "def fetch(config): return []", "ai_fallback_to_title": True},
    )
    client.post(
        "/config/adapters/csn",
        data={"mode": "edit", "adapter_type": "custom",
              "code": "def fetch(config): return []", "ai_on_failure": "use_title"},
        follow_redirects=False,
    )
    config = json.loads(get_adapter_instance(conn, "csn")["config"])
    assert "ai_fallback_to_title" not in config
    assert config["ai_on_failure"] == "use_title"


def test_adapter_edit_page_selects_ai_on_failure(client, conn):
    set_adapter_instance(
        conn, "csn", "custom",
        {"code": "def fetch(config): return []", "ai_on_failure": "abort"},
    )

    response = client.get("/config/adapters/csn/edit")

    assert response.status_code == 200
    assert '<option value="abort" selected' in response.text


def test_adapter_edit_page_maps_legacy_flag_to_use_title(client, conn):
    set_adapter_instance(
        conn, "csn", "custom",
        {"code": "def fetch(config): return []", "ai_fallback_to_title": True},
    )

    response = client.get("/config/adapters/csn/edit")

    assert '<option value="use_title" selected' in response.text


def test_adapter_new_page_offers_aiprompt_type(client):
    response = client.get("/config/adapters/new")

    assert response.status_code == 200
    # The type picker is 3 segmented pills (not a <select><option>) driving
    # a hidden #adapter_type input — see adapter_form.html's redesign.
    assert 'data-adapter-type="aiprompt"' in response.text
    assert 'id="aiprompt-fields"' in response.text


def test_adapter_create_persists_aiprompt_config(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "wx",
            "adapter_type": "aiprompt",
            "aip_prompt": "Weather report for {date}.",
            "aip_title_template": "Weather — {date}",
            "aip_type": "weather",
            "policy": "default",
            "use_ai": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    config = json.loads(get_adapter_instance(conn, "wx")["config"])
    # cron + policy now live on the assigned Policy, not the adapter config
    assert config == {
        "prompt": "Weather report for {date}.",
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


def test_adapter_edit_page_prefills_aiprompt_fields(client, conn):
    set_adapter_instance(
        conn,
        "wx",
        "aiprompt",
        {"prompt": "MARKER-PROMPT {date}", "type": "weather"},
    )

    response = client.get("/config/adapters/wx/edit")

    assert response.status_code == 200
    assert "MARKER-PROMPT {date}" in response.text
    assert 'value="weather"' in response.text


def test_adapter_create_persists_voice_replacements(client, conn):
    response = client.post(
        "/config/adapters",
        data={
            "mode": "create",
            "source": "vr-source",
            "adapter_type": "api",
            "enabled": "on",
            "url": "https://example.test/api",
            "method": "GET",
            "map_id_template": "{Id}",
            "vrepl_key": ["KM", "NE", "  "],
            "vrepl_value": ["Kilómetros", "Noreste", "dropped"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    config = json.loads(get_adapter_instance(conn, "vr-source")["config"])
    # blank-key row dropped, the rest kept verbatim
    assert config["voice_replacements"] == {"KM": "Kilómetros", "NE": "Noreste"}


def test_adapter_save_omits_voice_replacements_when_all_blank(client, conn):
    set_adapter_instance(
        conn, "vr2", "api",
        {"url": "https://x", "voice_replacements": {"KM": "Kilómetros"}},
    )

    response = client.post(
        "/config/adapters/vr2",
        data={
            "mode": "edit",
            "adapter_type": "api",
            "enabled": "on",
            "url": "https://x",
            "method": "GET",
            "map_id_template": "{Id}",
            "vrepl_key": [""],
            "vrepl_value": [""],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    config = json.loads(get_adapter_instance(conn, "vr2")["config"])
    assert "voice_replacements" not in config


def test_adapter_new_page_offers_suggested_replacement_set(client):
    response = client.get("/config/adapters/new")

    assert response.status_code == 200
    assert 'id="suggested-voice-replacements"' in response.text
    assert "kilómetros" in response.text  # the suggested-set JSON blob
    # a brand-new adapter starts with no saved rows
    assert 'name="vrepl_key" value=' not in response.text


def test_adapter_edit_page_prefills_saved_voice_replacements(client, conn):
    set_adapter_instance(
        conn, "vr3", "api",
        {"url": "https://x", "voice_replacements": {"SENAPRED": "Senapred"}},
    )

    response = client.get("/config/adapters/vr3/edit")

    assert response.status_code == 200
    assert 'name="vrepl_key" value="SENAPRED"' in response.text
    assert 'name="vrepl_value" value="Senapred"' in response.text


# --------------------------------- per-adapter export ---------------------------------


def test_adapter_export_download_is_an_attachment_with_expected_filename(client, conn):
    set_adapter_instance(conn, "demo", "api", {"url": "https://example.com"})

    response = client.get("/config/adapters/demo/export")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "radiobeacon-adapter-demo-" in response.headers["content-disposition"]
    data = response.json()
    assert data["kind"] == "radiobeacon.adapter_instance"
    assert data["adapter"]["source"] == "demo"


def test_adapter_export_download_404s_for_unknown_source(client):
    response = client.get("/config/adapters/does-not-exist/export")

    assert response.status_code == 404


# --------------------------------- per-adapter import ---------------------------------


def _upload_adapter(client, payload, url="/config/adapters/import/preview", **kwargs):
    files = {"upload": ("adapter.json", json.dumps(payload), "application/json")}
    return client.post(url, files=files, **kwargs)


def _adapter_payload(**over):
    base = {
        "kind": "radiobeacon.adapter_instance",
        "schema_version": 1,
        "adapter": {"source": "brand-new", "adapter_type": "api", "config": {"url": "https://x"}},
    }
    base.update(over)
    return base


def test_adapter_import_page_returns_200(client):
    response = client.get("/config/adapters/import")

    assert response.status_code == 200


def test_adapter_import_of_a_brand_new_source_applies_immediately_no_confirm_page(client, conn):
    payload = _adapter_payload()

    response = _upload_adapter(
        client, payload, url="/config/adapters/import/preview", follow_redirects=False
    )

    assert response.status_code == 303
    assert "/config/adapters" in response.headers["location"]
    assert get_adapter_instance(conn, "brand-new") is not None


def test_adapter_import_of_an_existing_source_renders_confirm_page(client, conn):
    set_adapter_instance(conn, "csn", "api", {"url": "https://old"})
    payload = _adapter_payload(
        adapter={"source": "csn", "adapter_type": "api", "config": {"url": "https://new"}}
    )

    response = _upload_adapter(client, payload)

    assert response.status_code == 200
    assert "config_json" in response.text
    assert 'name="confirm_overwrite"' in response.text
    # not applied yet
    assert json.loads(get_adapter_instance(conn, "csn")["config"])["url"] == "https://old"


def test_adapter_import_apply_without_overwrite_confirmation_is_rejected(client, conn):
    set_adapter_instance(conn, "csn", "api", {"url": "https://old"})
    payload = _adapter_payload(
        adapter={"source": "csn", "adapter_type": "api", "config": {"url": "https://new"}}
    )

    response = client.post(
        "/config/adapters/import/apply", data={"config_json": json.dumps(payload)}
    )

    assert response.status_code == 400
    assert json.loads(get_adapter_instance(conn, "csn")["config"])["url"] == "https://old"


def test_adapter_import_apply_with_overwrite_confirmation_upserts(client, conn):
    set_adapter_instance(conn, "csn", "api", {"url": "https://old"})
    payload = _adapter_payload(
        adapter={"source": "csn", "adapter_type": "api", "config": {"url": "https://new"}}
    )

    response = client.post(
        "/config/adapters/import/apply",
        data={"config_json": json.dumps(payload), "confirm_overwrite": "on"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert json.loads(get_adapter_instance(conn, "csn")["config"])["url"] == "https://new"


def test_adapter_import_custom_type_requires_code_review_checkbox_when_dev_tools_on(client, conn):
    payload = _adapter_payload(
        adapter={"source": "reviewed", "adapter_type": "custom",
                 "config": {"code": "def fetch(config): return []"}}
    )

    preview = _upload_adapter(client, payload)
    assert preview.status_code == 200
    assert 'name="confirm_custom_code"' in preview.text

    rejected = client.post(
        "/config/adapters/import/apply", data={"config_json": json.dumps(payload)}
    )
    assert rejected.status_code == 400
    assert get_adapter_instance(conn, "reviewed") is None

    applied = client.post(
        "/config/adapters/import/apply",
        data={"config_json": json.dumps(payload), "confirm_custom_code": "on"},
        follow_redirects=False,
    )
    assert applied.status_code == 303
    assert get_adapter_instance(conn, "reviewed") is not None


def test_adapter_import_custom_type_rejected_when_dev_tools_off(client, conn):
    set_setting(conn, "UI_DEV_TOOLS_ENABLED", "false")
    payload = _adapter_payload(
        adapter={"source": "sneaky", "adapter_type": "custom",
                 "config": {"code": "def fetch(config): return []"}}
    )

    response = client.post(
        "/config/adapters/import/apply", data={"config_json": json.dumps(payload)}
    )

    assert response.status_code == 400
    assert get_adapter_instance(conn, "sneaky") is None


def test_adapter_import_override_source_field_imports_under_new_key(client, conn):
    payload = _adapter_payload(
        adapter={"source": "original", "adapter_type": "api", "config": {"url": "https://x"}}
    )

    response = client.post(
        "/config/adapters/import/apply",
        data={"config_json": json.dumps(payload), "override_source": "original-copy"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_adapter_instance(conn, "original-copy") is not None
    assert get_adapter_instance(conn, "original") is None


def test_adapter_test_action_does_not_block_other_requests(client):
    """Regression test for the bug this whole change fixes: adapter.fetch()
    used to run synchronously on the single asyncio event loop this
    dashboard's one uvicorn worker shares for every request (see
    ui/src/ui/tx_stream.py's single-worker note), so a slow test fetch
    froze every other user's request until it finished. adapters.py now
    runs it via starlette.concurrency.run_in_threadpool.

    Plain TestClient calls (as every other test in this file uses them)
    each get their own throwaway event loop per call (see
    starlette.testclient.TestClient._portal_factory), which would make
    this test pass even without the fix -- it wouldn't be exercising a
    shared event loop at all. Setting `.portal` directly, instead of
    entering the client as `with client:`, gets that one-shared-event-loop
    behavior (matching production) without also running this app's real
    lifespan handler, which opens the real on-disk default database (not
    this test's in-memory `conn`) to bootstrap an admin account -- exactly
    the kind of side effect a test using an in-memory conn should not
    trigger.
    """
    import threading
    import time

    import anyio.from_thread

    slow_done = threading.Event()
    result: dict = {}

    def run_slow_test():
        client.post(
            "/config/adapters/test",
            data={
                "mode": "create",
                "source": "slow-test-source",
                "adapter_type": "custom",
                "code": (
                    "import time\n"
                    "def fetch(config):\n"
                    "    time.sleep(1.5)\n"
                    "    return [{'id': '1', 'title': 'slow'}]\n"
                ),
            },
        )
        slow_done.set()

    with anyio.from_thread.start_blocking_portal(**client.async_backend) as portal:
        client.portal = portal
        try:
            thread = threading.Thread(target=run_slow_test)
            thread.start()
            # Give the slow request a moment to actually reach and start
            # executing adapter.fetch() before racing the second request.
            time.sleep(0.3)

            start = time.monotonic()
            result["status"] = client.get("/config/adapters").status_code
            result["elapsed"] = time.monotonic() - start
            # If adapter.fetch() still ran inline on the shared event loop,
            # the slow test-fetch would have to finish first.
            result["slow_done_before_other_finished"] = slow_done.is_set()

            thread.join(timeout=5)
        finally:
            client.portal = None

    assert result["status"] == 200
    assert not result["slow_done_before_other_finished"]
    assert result["elapsed"] < 1.0
    assert slow_done.is_set()
