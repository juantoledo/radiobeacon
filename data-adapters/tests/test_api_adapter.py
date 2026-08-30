import json
from unittest.mock import patch

from adapters.api_adapter import ApiAdapter, preview_response

CSN_CONFIG = {
    "url": "https://api.gael.cloud/general/public/sismos",
    "method": "GET",
    "headers": {"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    "items_path": "",
    "mapping": {
        "id": {"field": "Fecha", "field_date_format": "%Y-%m-%d %H:%M:%S"},
        "event_key": {"field": "Fecha", "field_date_format": "%Y-%m-%d %H:%M:%S"},
        "title": {"template": "Sismo M{Magnitud} - {RefGeografica}"},
        "contents": {
            "template": "Sismo de magnitud {Magnitud}, profundidad {Profundidad} km, {RefGeografica}."
        },
        "url": {"value": "https://www.sismologia.cl/"},
        "type": {"value": "Sismo"},
    },
    "date_field": "Fecha",
    "date_format": "%Y-%m-%d %H:%M:%S",
    "source_timezone": "America/Santiago",
    "transmit_policy_rule": {
        "field": "Magnitud",
        "operator": ">=",
        "threshold": 4.5,
        "if_true": "urgent",
        "if_false": "informational",
    },
}

CSN_RESPONSE = [
    {
        "Fecha": "2026-01-01 10:00:00",
        "Profundidad": "10.0",
        "Magnitud": "5.0",
        "RefGeografica": "Test Zone",
    },
    {
        "Fecha": "2026-01-01 11:00:00",
        "Profundidad": "5.0",
        "Magnitud": "3.0",
        "RefGeografica": "Other Zone",
    },
]


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_maps_fields_from_config():
    with patch("urllib.request.urlopen", return_value=_FakeResponse(CSN_RESPONSE)):
        reading = ApiAdapter("csn", CSN_CONFIG).fetch()

    assert reading.ok
    assert len(reading.data) == 2
    newest, oldest = reading.data  # sorted newest-first
    assert newest.id == "2026-01-01T11:00:00"
    assert newest.event_key == "2026-01-01T11:00:00"
    assert newest.title == "Sismo M3.0 - Other Zone"
    assert newest.url == "https://www.sismologia.cl/"
    assert newest.type == "Sismo"
    assert newest.transmit_policy == "informational"  # Magnitud 3.0 < 4.5 threshold
    assert oldest.transmit_policy == "urgent"  # Magnitud 5.0 >= 4.5 threshold
    assert oldest.raw["Magnitud"] == "5.0"


def test_fetch_converts_source_date_time_with_configured_timezone():
    with patch("urllib.request.urlopen", return_value=_FakeResponse(CSN_RESPONSE[:1])):
        reading = ApiAdapter("csn", CSN_CONFIG).fetch()

    # America/Santiago is UTC-3 (or -4) — the converted UTC hour must differ
    # from the raw "10:00:00" local value.
    assert reading.data[0].source_date_time.hour != 10


def test_fetch_handles_items_path():
    config = dict(CSN_CONFIG, items_path="result.items")
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse({"result": {"items": CSN_RESPONSE[:1]}}),
    ):
        reading = ApiAdapter("csn", config).fetch()

    assert reading.ok
    assert len(reading.data) == 1


def test_fetch_returns_not_ok_on_network_error():
    import urllib.error

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        reading = ApiAdapter("csn", CSN_CONFIG).fetch()

    assert not reading.ok
    assert reading.error


def test_fetch_skips_malformed_item_without_failing_whole_batch():
    bad_and_good = [{"Fecha": None}, CSN_RESPONSE[0]]
    with patch("urllib.request.urlopen", return_value=_FakeResponse(bad_and_good)):
        reading = ApiAdapter("csn", CSN_CONFIG).fetch()

    assert reading.ok
    assert len(reading.data) == 1


def test_preview_response_needs_no_mapping_configured():
    config = {"url": "https://example.test/", "headers": {}}
    with patch("urllib.request.urlopen", return_value=_FakeResponse(CSN_RESPONSE)):
        preview = preview_response(config)

    assert preview["items"] == CSN_RESPONSE
    assert preview["is_list"] is True
    assert preview["items_path_resolved"] is True


def test_preview_response_honors_items_path_and_limit():
    config = {"url": "https://example.test/", "items_path": "result.items"}
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse({"result": {"items": CSN_RESPONSE}}),
    ):
        preview = preview_response(config, limit=1)

    assert preview["items"] == CSN_RESPONSE[:1]
    assert preview["is_list"] is True


def test_preview_response_reports_non_list_items_path_without_raising():
    config = {"url": "https://example.test/", "items_path": "result"}
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse({"result": {"a": 1}}),
    ):
        preview = preview_response(config)

    assert preview["items_path_resolved"] is True
    assert preview["is_list"] is False
    assert preview["items"] == [{"a": 1}]


def test_preview_response_reports_unresolvable_items_path_without_raising():
    config = {"url": "https://example.test/", "items_path": "does.not.exist"}
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse({"result": {"a": 1}}),
    ):
        preview = preview_response(config)

    assert preview["items_path_resolved"] is False
    assert preview["is_list"] is False
    assert preview["items"] == []


def test_preview_response_finds_array_candidates_at_any_depth():
    config = {"url": "https://example.test/"}
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse({"result": {"items": CSN_RESPONSE}, "meta": {"count": 2}}),
    ):
        preview = preview_response(config)

    paths = {c["path"]: c["count"] for c in preview["candidates"]}
    assert paths == {"result.items": 2}
    candidate = preview["candidates"][0]
    assert candidate["sample_keys"] == ["Fecha", "Magnitud", "Profundidad", "RefGeografica"]


def test_preview_response_root_array_candidate_has_empty_path():
    config = {"url": "https://example.test/"}
    with patch("urllib.request.urlopen", return_value=_FakeResponse(CSN_RESPONSE)):
        preview = preview_response(config)

    assert preview["candidates"] == [
        {
            "path": "",
            "count": 2,
            "sample_keys": ["Fecha", "Magnitud", "Profundidad", "RefGeografica"],
        }
    ]


def test_preview_response_ignores_arrays_of_bare_scalars():
    config = {"url": "https://example.test/"}
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse({"tags": ["a", "b", "c"], "items": [{"id": 1}]}),
    ):
        preview = preview_response(config)

    paths = [c["path"] for c in preview["candidates"]]
    assert paths == ["items"]
