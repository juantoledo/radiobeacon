import json
from unittest.mock import patch

from adapters.storage import get_adapter_instance, get_source_fields, set_adapter_instance


def test_adapters_list_returns_200_and_shows_seeded_instances(client):
    response = client.get("/adapters")

    assert response.status_code == 200
    assert "csn" in response.text
    assert "senapred" in response.text


def test_nav_shows_adapters_link(client):
    response = client.get("/")

    assert 'href="/adapters"' in response.text


def test_adapter_new_page_returns_200(client):
    response = client.get("/adapters/new")

    assert response.status_code == 200


def test_adapter_edit_page_returns_200_for_known_source(client):
    response = client.get("/adapters/csn/edit")

    assert response.status_code == 200
    # CSN's seeded config prefills the structured fields, not raw JSON.
    assert 'value="https://api.gael.cloud/general/public/sismos"' in response.text
    assert 'name="map_title_template" value="Sismo M{Magnitud} - {RefGeografica}"' in response.text


def test_adapter_edit_page_404s_for_unknown_source(client):
    response = client.get("/adapters/does-not-exist/edit")

    assert response.status_code == 404


def test_adapter_create_persists_structured_api_config(client, conn):
    response = client.post(
        "/adapters",
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
            "transmit_enabled": "on",
            "transmit_field": "score",
            "transmit_operator": ">=",
            "transmit_threshold": "5",
            "transmit_if_true": "urgent",
            "transmit_if_false": "informational",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/adapters?msg=")

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
    assert config["transmit_policy_rule"] == {
        "field": "score", "operator": ">=", "threshold": 5.0,
        "if_true": "urgent", "if_false": "informational",
    }
    assert get_source_fields(conn, "new-source") == {
        "source_name": "New Source", "source_url": "https://example.test/",
    }


def test_adapter_create_custom_type_config_is_only_code(client, conn):
    response = client.post(
        "/adapters",
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
        "/adapters",
        data={"mode": "create", "source": "broken", "adapter_type": "api", "url": ""},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert get_adapter_instance(conn, "broken") is None


def test_adapter_create_rejects_invalid_transmit_threshold(client, conn):
    response = client.post(
        "/adapters",
        data={
            "mode": "create",
            "source": "broken",
            "adapter_type": "api",
            "url": "https://example.test/",
            "transmit_enabled": "on",
            "transmit_field": "score",
            "transmit_threshold": "not-a-number",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert get_adapter_instance(conn, "broken") is None
    # What the user typed must survive the error re-render.
    assert 'value="https://example.test/"' in response.text


def test_adapter_edit_updates_existing_instance(client, conn):
    response = client.post(
        "/adapters/csn",
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
    response = client.post("/adapters/csn/delete", follow_redirects=False)

    assert response.status_code == 303
    assert get_adapter_instance(conn, "csn") is None


def test_adapter_delete_unknown_source_still_redirects(client):
    response = client.post("/adapters/does-not-exist/delete", follow_redirects=False)

    assert response.status_code == 303


def test_adapter_test_action_runs_fetch_and_shows_result(client):
    response = client.post(
        "/adapters/test",
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
        "/adapters/test",
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
        "/adapters/test",
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
            "/adapters/test",
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
        "/adapters/test",
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
            "/adapters/sample",
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
            "/adapters/sample",
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
            "/adapters/sample",
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
            "/adapters/sample",
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
            "/adapters/sample",
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
            "/adapters/sample",
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
        "/adapters/sample",
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
            "/adapters/sample",
            data={
                "mode": "create",
                "source": "not-saved",
                "adapter_type": "api",
                "url": "https://api.example.test/",
            },
        )

    assert get_adapter_instance(conn, "not-saved") is None
