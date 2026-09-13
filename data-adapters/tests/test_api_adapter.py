import json
from datetime import datetime, timezone
from unittest.mock import patch

from adapters.api_adapter import ApiAdapter, preview_response
from adapters.storage import get_connection, set_source

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

    def read(self, amt=None):
        return self._body if amt is None else self._body[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_maps_fields_from_config():
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE)):
        reading = ApiAdapter("csn", CSN_CONFIG).fetch()

    assert reading.ok
    assert len(reading.data) == 2
    newest, oldest = reading.data  # sorted newest-first
    assert newest.id == "2026-01-01T11:00:00"
    assert newest.event_key == "2026-01-01T11:00:00"
    assert newest.title == "Sismo M3.0 - Other Zone"
    assert newest.url == "https://www.sismologia.cl/"
    assert newest.type == "Sismo"
    assert oldest.raw["Magnitud"] == "5.0"


def test_fetch_converts_source_date_time_with_configured_timezone():
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE[:1])):
        reading = ApiAdapter("csn", CSN_CONFIG).fetch()

    # America/Santiago is UTC-3 (or -4) — the converted UTC hour must differ
    # from the raw "10:00:00" local value.
    assert reading.data[0].source_date_time.hour != 10


def test_fetch_handles_items_path():
    config = dict(CSN_CONFIG, items_path="result.items")
    with patch(
        "adapters.api_adapter._OPENER.open",
        return_value=_FakeResponse({"result": {"items": CSN_RESPONSE[:1]}}),
    ):
        reading = ApiAdapter("csn", config).fetch()

    assert reading.ok
    assert len(reading.data) == 1


def test_fetch_returns_not_ok_on_network_error():
    import urllib.error

    with patch("adapters.api_adapter._OPENER.open", side_effect=urllib.error.URLError("boom")):
        reading = ApiAdapter("csn", CSN_CONFIG).fetch()

    assert not reading.ok
    assert reading.error


def test_fetch_treats_object_items_path_result_as_a_single_item():
    # A response whose root (or items_path target) is a JSON object, not a
    # list — used to be iterated as a dict (yielding its keys as bogus
    # "items" and logging a "'str' object is not a mapping" error per key,
    # producing zero items). Now treated as exactly one item, the same way
    # preview_response already displays it — this is what lets a
    # single-object-response source (e.g. "current weather" APIs) work
    # with items_path left empty, including via the whole-response
    # {response} placeholder (see FieldMapping.resolve).
    config = dict(
        CSN_CONFIG,
        items_path="",
        mapping=dict(
            CSN_CONFIG["mapping"],
            id={"template": "openmeteo-{uuid}"},
            contents={"template": "{response}"},
        ),
    )
    response = {"latitude": -33.6, "hourly": {"temperature_2m": [1, 2]}}
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(response)):
        reading = ApiAdapter("csn", config).fetch()

    assert reading.ok
    assert len(reading.data) == 1
    assert json.loads(reading.data[0].contents) == response


def test_fetch_fails_cleanly_when_items_path_resolves_to_a_scalar():
    # Neither a list nor a dict — genuinely can't be treated as any item(s).
    config = dict(CSN_CONFIG, items_path="latitude")
    with patch(
        "adapters.api_adapter._OPENER.open",
        return_value=_FakeResponse({"latitude": -33.6}),
    ):
        reading = ApiAdapter("csn", config).fetch()

    assert not reading.ok
    assert "not a list" in reading.error
    assert reading.data == []


def test_fetch_skips_malformed_item_without_failing_whole_batch():
    bad_and_good = [{"Fecha": None}, CSN_RESPONSE[0]]
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(bad_and_good)):
        reading = ApiAdapter("csn", CSN_CONFIG).fetch()

    assert reading.ok
    assert len(reading.data) == 1


def test_fetch_skips_item_with_broken_dotted_mapping_path():
    # One item is missing the nested field a dotted contents template
    # references — it's skipped, but a sibling item that has the field
    # still comes through (same graceful-batch behavior as a missing id).
    config = dict(
        CSN_CONFIG,
        mapping=dict(CSN_CONFIG["mapping"], contents={"template": "{estacion.nombre}"}),
    )
    good_item = dict(CSN_RESPONSE[0], estacion={"nombre": "Alpha"})
    bad_item = dict(CSN_RESPONSE[1], estacion={"otro": "sin nombre"})
    with patch(
        "adapters.api_adapter._OPENER.open", return_value=_FakeResponse([good_item, bad_item])
    ):
        reading = ApiAdapter("csn", config).fetch()

    assert reading.ok
    assert len(reading.data) == 1
    assert reading.data[0].contents == "Alpha"


def test_fetch_mapping_template_can_reference_source_display_name(tmp_path):
    db_path = tmp_path / "radiobeacon.db"
    conn = get_connection(db_path)
    set_source(conn, "quakes", "ACME Quake Feed", "https://acme.example")
    conn.close()

    config = dict(
        CSN_CONFIG,
        mapping=dict(
            CSN_CONFIG["mapping"],
            title={"template": "{source_name}: Sismo M{Magnitud} - {RefGeografica}"},
            url={"template": "{source_url}"},
        ),
    )
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE[:1])):
        reading = ApiAdapter("quakes", config, db_path=db_path).fetch()

    assert reading.data[0].title == "ACME Quake Feed: Sismo M5.0 - Test Zone"
    assert reading.data[0].url == "https://acme.example"


def test_fetch_mapping_source_name_falls_back_to_raw_key_when_unmanaged(tmp_path):
    db_path = tmp_path / "radiobeacon.db"
    get_connection(db_path).close()  # schema only, no `sources` row for "quakes"

    config = dict(
        CSN_CONFIG,
        mapping=dict(CSN_CONFIG["mapping"], title={"template": "[{source_name}] {RefGeografica}"}),
    )
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE[:1])):
        reading = ApiAdapter("quakes", config, db_path=db_path).fetch()

    assert reading.data[0].title == "[quakes] Test Zone"


def test_fetch_mapping_template_can_reference_whole_item_as_json():
    config = dict(
        CSN_CONFIG,
        mapping=dict(CSN_CONFIG["mapping"], contents={"template": "{raw}"}),
    )
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE)):
        reading = ApiAdapter("csn", config).fetch()

    assert reading.ok
    # fetch() sorts by source_date_time descending, so match each mapped
    # item back to its source row by Fecha rather than assuming order.
    by_fecha = {row["Fecha"]: row for row in CSN_RESPONSE}
    for mapped in reading.data:
        parsed = json.loads(mapped.contents)
        assert parsed == by_fecha[parsed["Fecha"]]
    assert {json.loads(m.contents)["Fecha"] for m in reading.data} == set(by_fecha)


def test_fetch_mapping_raw_falls_back_to_real_item_key_when_present():
    config = dict(
        CSN_CONFIG,
        mapping=dict(CSN_CONFIG["mapping"], contents={"template": "{raw}"}),
    )
    response = [dict(CSN_RESPONSE[0], raw="already present")]
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(response)):
        reading = ApiAdapter("csn", config).fetch()

    assert reading.data[0].contents == "already present"


def test_fetch_mapping_template_can_reference_whole_response_as_json():
    config = dict(
        CSN_CONFIG,
        mapping=dict(CSN_CONFIG["mapping"], contents={"template": "{response}"}),
    )
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE)):
        reading = ApiAdapter("csn", config).fetch()

    assert reading.ok
    # items_path="" — the raw response IS the full list, same for every item.
    assert json.loads(reading.data[0].contents) == CSN_RESPONSE
    assert json.loads(reading.data[1].contents) == CSN_RESPONSE


def test_fetch_mapping_response_falls_back_to_real_item_key_when_present():
    config = dict(
        CSN_CONFIG,
        mapping=dict(CSN_CONFIG["mapping"], contents={"template": "{response}"}),
    )
    response = [dict(CSN_RESPONSE[0], response="already present")]
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(response)):
        reading = ApiAdapter("csn", config).fetch()

    assert reading.data[0].contents == "already present"


def test_fetch_mapping_template_can_reference_current_date_and_datetime():
    fixed_now = datetime(2026, 3, 5, 12, 30, 0, tzinfo=timezone.utc)
    config = dict(
        CSN_CONFIG,
        mapping=dict(CSN_CONFIG["mapping"], contents={"template": "{date} / {datetime}"}),
    )
    with patch("adapters.api_adapter.utc_now", return_value=fixed_now):
        with patch(
            "adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE[:1])
        ):
            reading = ApiAdapter("csn", config).fetch()

    assert reading.ok
    assert reading.data[0].contents == "2026-03-05 / 2026-03-05T12:30:00+00:00"


def test_fetch_date_field_can_be_set_to_current_datetime():
    # date_field is normally a real item key, but {datetime}/{date} are
    # pickable there too (see ApiAdapterConfig.resolve_source_date_time) —
    # useful for a source with no per-item timestamp of its own, where
    # every item gets stamped with this fetch's own start time instead.
    fixed_now = datetime(2026, 3, 5, 12, 30, 0, tzinfo=timezone.utc)
    config = dict(CSN_CONFIG, date_field="datetime", date_format=None)
    with patch("adapters.api_adapter.utc_now", return_value=fixed_now):
        with patch(
            "adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE[:1])
        ):
            reading = ApiAdapter("csn", config).fetch()

    assert reading.ok
    assert reading.data[0].source_date_time == fixed_now


def test_fetch_date_field_current_datetime_falls_back_to_real_item_key_when_present():
    config = dict(CSN_CONFIG, date_field="datetime", date_format=None)
    response = [dict(CSN_RESPONSE[0], datetime="2020-01-01T00:00:00+00:00")]
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(response)):
        reading = ApiAdapter("csn", config).fetch()

    assert reading.ok
    assert reading.data[0].source_date_time == datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_fetch_does_not_touch_db_when_no_template_uses_source_placeholders(tmp_path):
    """The common case — no mapping references {source_name}/{source_url} —
    keeps the fetch path DB-free."""
    with patch("adapters.api_adapter.get_connection", side_effect=AssertionError("db opened")):
        with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE[:1])):
            reading = ApiAdapter("csn", CSN_CONFIG).fetch()

    assert reading.ok
    assert reading.data[0].title == "Sismo M5.0 - Test Zone"


def test_preview_response_needs_no_mapping_configured():
    config = {"url": "https://example.test/", "headers": {}}
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE)):
        preview = preview_response(config)

    assert preview["items"] == CSN_RESPONSE
    assert preview["is_list"] is True
    assert preview["items_path_resolved"] is True


def test_preview_response_honors_items_path_and_limit():
    config = {"url": "https://example.test/", "items_path": "result.items"}
    with patch(
        "adapters.api_adapter._OPENER.open",
        return_value=_FakeResponse({"result": {"items": CSN_RESPONSE}}),
    ):
        preview = preview_response(config, limit=1)

    assert preview["items"] == CSN_RESPONSE[:1]
    assert preview["is_list"] is True


def test_preview_response_reports_non_list_items_path_without_raising():
    config = {"url": "https://example.test/", "items_path": "result"}
    with patch(
        "adapters.api_adapter._OPENER.open",
        return_value=_FakeResponse({"result": {"a": 1}}),
    ):
        preview = preview_response(config)

    assert preview["items_path_resolved"] is True
    assert preview["is_list"] is False
    assert preview["items"] == [{"a": 1}]


def test_preview_response_reports_unresolvable_items_path_without_raising():
    config = {"url": "https://example.test/", "items_path": "does.not.exist"}
    with patch(
        "adapters.api_adapter._OPENER.open",
        return_value=_FakeResponse({"result": {"a": 1}}),
    ):
        preview = preview_response(config)

    assert preview["items_path_resolved"] is False
    assert preview["is_list"] is False
    assert preview["items"] == []


def test_preview_response_finds_array_candidates_at_any_depth():
    config = {"url": "https://example.test/"}
    with patch(
        "adapters.api_adapter._OPENER.open",
        return_value=_FakeResponse({"result": {"items": CSN_RESPONSE}, "meta": {"count": 2}}),
    ):
        preview = preview_response(config)

    paths = {c["path"]: c["count"] for c in preview["candidates"]}
    assert paths == {"result.items": 2}
    candidate = preview["candidates"][0]
    assert candidate["sample_keys"] == ["Fecha", "Magnitud", "Profundidad", "RefGeografica"]


def test_preview_response_root_array_candidate_has_empty_path():
    config = {"url": "https://example.test/"}
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE)):
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
        "adapters.api_adapter._OPENER.open",
        return_value=_FakeResponse({"tags": ["a", "b", "c"], "items": [{"id": 1}]}),
    ):
        preview = preview_response(config)

    paths = [c["path"] for c in preview["candidates"]]
    assert paths == ["items"]


# --- SSRF / resource guards (adapters.api_adapter._assert_fetch_allowed,
#     _SsrfGuardHandler, the hand-built _OPENER, the response size cap) ---


def test_fetch_rejects_file_scheme():
    reading = ApiAdapter("x", {"url": "file:///etc/passwd"}).fetch()
    assert not reading.ok
    assert "scheme" in reading.error


def test_fetch_rejects_loopback_address():
    reading = ApiAdapter("x", {"url": "http://127.0.0.1:8080/"}).fetch()
    assert not reading.ok
    assert "non-public" in reading.error


def test_fetch_rejects_link_local_metadata_address():
    reading = ApiAdapter("x", {"url": "http://169.254.169.254/latest/meta-data/"}).fetch()
    assert not reading.ok
    assert "non-public" in reading.error


def test_allow_private_setting_opens_loopback(tmp_path):
    from adapters.storage import get_connection, set_setting

    db_path = tmp_path / "radiobeacon.db"
    conn = get_connection(db_path)
    set_setting(conn, "ADAPTERS_ALLOW_PRIVATE_FETCH", "true")
    conn.close()

    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse([{"id": 1}])):
        reading = ApiAdapter("x", {"url": "http://127.0.0.1:9999/"}, db_path=db_path).fetch()

    assert reading.ok  # the guard was not consulted; the (mocked) open succeeded


def test_fetch_rejects_oversize_response():
    huge = [{"id": i, "pad": "x" * 1024} for i in range(6000)]
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(huge)):
        reading = ApiAdapter("x", CSN_CONFIG).fetch()
    assert not reading.ok
    assert "bytes" in reading.error


# --------------------------------- response_format: xml ---------------------------------


class _FakeXmlResponse:
    def __init__(self, xml_str: str):
        self._body = xml_str.encode("utf-8")

    def read(self, amt=None):
        return self._body if amt is None else self._body[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


XML_CONFIG = {
    "url": "https://api.example.com/quakes.xml",
    "method": "GET",
    "response_format": "xml",
    "items_path": "quakes.quake",
    "mapping": {
        "id": {"template": "{@id}"},
        "title": {"template": "M{magnitude} - {location.name}"},
        "contents": {"template": "M{magnitude} at {location.name}"},
    },
}

XML_RESPONSE = """<?xml version="1.0"?>
<quakes>
  <quake id="q1">
    <magnitude>5.0</magnitude>
    <location><name>Test Zone</name></location>
  </quake>
  <quake id="q2">
    <magnitude>3.0</magnitude>
    <location><name>Other Zone</name></location>
  </quake>
</quakes>
"""


def test_fetch_parses_xml_response_with_attribute_and_nested_element_mapping():
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeXmlResponse(XML_RESPONSE)):
        reading = ApiAdapter("quakes", XML_CONFIG).fetch()

    assert reading.ok
    assert len(reading.data) == 2
    ids = {item.id for item in reading.data}
    assert ids == {"q1", "q2"}
    titles = {item.title for item in reading.data}
    assert titles == {"M5.0 - Test Zone", "M3.0 - Other Zone"}


def test_fetch_reports_malformed_xml_as_a_failed_reading():
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeXmlResponse("<not-closed>")):
        reading = ApiAdapter("quakes", XML_CONFIG).fetch()

    assert not reading.ok
    assert reading.error


def test_fetch_defaults_to_json_when_response_format_absent():
    config = {**CSN_CONFIG}
    config.pop("response_format", None)
    with patch("adapters.api_adapter._OPENER.open", return_value=_FakeResponse(CSN_RESPONSE)):
        reading = ApiAdapter("csn", config).fetch()

    assert reading.ok
    assert len(reading.data) == 2
