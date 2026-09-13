import uuid

import pytest

from adapters.api_adapter import ApiAdapterConfig, FieldMapping


def test_field_mapping_template_renders_against_item():
    mapping = FieldMapping(template="{a}-{b}")

    assert mapping.resolve({"a": "1", "b": "2"}, "x") == "1-2"


def test_field_mapping_bare_placeholder_template_is_a_raw_passthrough():
    """A template that's just `{a}` and nothing else is exactly what the
    old "raw field" strategy did — there's no separate mode for it."""
    mapping = FieldMapping(template="{a}")

    assert mapping.resolve({"a": "raw value"}, "x") == "raw value"


def test_field_mapping_no_placeholder_template_is_a_constant():
    """A template with no `{...}` at all is exactly what the old
    "constant value" strategy did."""
    mapping = FieldMapping(template="a constant string")

    assert mapping.resolve({"a": "ignored"}, "x") == "a constant string"


def test_field_mapping_field_date_format_reformats_as_iso_no_tz_shift():
    mapping = FieldMapping(template="{a}", field_date_format="%Y-%m-%d %H:%M:%S")

    assert mapping.resolve({"a": "2026-01-01 10:00:00"}, "x") == "2026-01-01T10:00:00"


def test_field_mapping_field_date_format_ignored_unless_template_is_a_bare_placeholder():
    """field_date_format only applies to a single bare {field} template —
    a multi-field construction or a constant can't be "the raw value,
    reformatted", so the date parsing step is skipped and the template
    just renders normally (and would fail loudly rather than silently
    misparse if it tried strptime on a constructed string)."""
    mapping = FieldMapping(template="prefix-{a}", field_date_format="%Y-%m-%d %H:%M:%S")

    assert mapping.resolve({"a": "2026-01-01 10:00:00"}, "x") == "prefix-2026-01-01 10:00:00"


def test_field_mapping_from_dict_migrates_old_field_strategy():
    """The old {field, field_date_format} shape (predating the
    single-template redesign) must still resolve exactly as before, since
    already-saved adapter_instances rows use it."""
    mapping = FieldMapping.from_dict({"field": "a", "field_date_format": "%Y-%m-%d %H:%M:%S"})

    assert mapping.template == "{a}"
    assert mapping.resolve({"a": "2026-01-01 10:00:00"}, "x") == "2026-01-01T10:00:00"


def test_field_mapping_from_dict_migrates_old_value_strategy():
    mapping = FieldMapping.from_dict({"value": "a constant"})

    assert mapping.resolve({}, "x") == "a constant"


def test_field_mapping_from_dict_migrates_old_value_strategy_escapes_literal_braces():
    """A constant that happened to contain a literal brace must survive
    being represented as a template unchanged, not be misread as a
    placeholder."""
    mapping = FieldMapping.from_dict({"value": "literal {braces} here"})

    assert mapping.resolve({}, "x") == "literal {braces} here"


def test_field_mapping_from_dict_prefers_template_over_legacy_keys():
    mapping = FieldMapping.from_dict({"template": "{a}", "field": "b", "value": "c"})

    assert mapping.template == "{a}"


def test_field_mapping_template_uuid_placeholder_is_a_valid_uuid():
    mapping = FieldMapping(template="{uuid}")

    result = mapping.resolve({"a": "1"}, "id")

    assert uuid.UUID(result)  # raises ValueError if not a valid UUID string


def test_field_mapping_template_uuid_is_stable_for_the_same_item_content():
    mapping = FieldMapping(template="{uuid}")
    item = {"Fecha": "2026-01-01 10:00:00", "Magnitud": "3.0"}

    assert mapping.resolve(item, "id") == mapping.resolve(dict(item), "id")


def test_field_mapping_template_uuid_differs_for_different_item_content():
    mapping = FieldMapping(template="{uuid}")

    first = mapping.resolve({"Fecha": "2026-01-01 10:00:00"}, "id")
    second = mapping.resolve({"Fecha": "2026-01-01 11:00:00"}, "id")

    assert first != second


def test_field_mapping_template_uuid_yields_to_a_real_uuid_field_on_the_item():
    mapping = FieldMapping(template="{uuid}")

    result = mapping.resolve({"uuid": "a-real-source-provided-value"}, "id")

    assert result == "a-real-source-provided-value"


def test_field_mapping_returns_none_when_unconfigured():
    assert FieldMapping().resolve({"a": "1"}, "x") is None


def test_field_mapping_from_dict_defaults_to_empty():
    mapping = FieldMapping.from_dict(None)

    assert mapping == FieldMapping()


def test_api_adapter_config_from_dict_minimal():
    cfg = ApiAdapterConfig.from_dict({"url": "https://example.test/"})

    assert cfg.url == "https://example.test/"
    assert cfg.method == "GET"
    assert cfg.response_format == "json"
    assert cfg.headers == {}
    assert cfg.query_params == {}
    assert cfg.items_path == ""
    assert cfg.mapping == {}


def test_api_adapter_config_from_dict_full():
    cfg = ApiAdapterConfig.from_dict(
        {
            "url": "https://example.test/",
            "method": "POST",
            "response_format": "xml",
            "headers": {"Accept": "application/json"},
            "query_params": {"key": "value"},
            "body": "{}",
            "items_path": "result.items",
            "mapping": {"id": {"field": "Id"}, "title": {"value": "fixed"}},
            "date_field": "Date",
            "date_format": "%Y-%m-%d",
            "source_timezone": "America/Santiago",
        }
    )

    assert cfg.method == "POST"
    assert cfg.response_format == "xml"
    assert cfg.headers == {"Accept": "application/json"}
    assert cfg.query_params == {"key": "value"}
    assert cfg.body == "{}"
    assert cfg.items_path == "result.items"
    assert cfg.mapping_for("id") == FieldMapping(template="{Id}")
    assert cfg.mapping_for("title") == FieldMapping(template="fixed")
    assert cfg.mapping_for("contents") == FieldMapping()  # unconfigured -> empty, not KeyError


def test_api_adapter_config_response_format_defaults_to_json_when_absent():
    cfg = ApiAdapterConfig.from_dict({"url": "https://example.test/"})

    assert cfg.response_format == "json"


def test_api_adapter_config_response_format_round_trips():
    cfg = ApiAdapterConfig.from_dict({"url": "https://example.test/", "response_format": "xml"})

    assert cfg.response_format == "xml"


def test_field_mapping_template_supports_nested_dotted_path():
    mapping = FieldMapping(template="{estacion.nombre}")

    assert mapping.resolve({"estacion": {"nombre": "Alpha"}}, "x") == "Alpha"


def test_field_mapping_template_dotted_path_uses_first_element_of_nested_collection():
    mapping = FieldMapping(template="{estacion.nombre}")
    item = {"estacion": [{"nombre": "Alpha"}, {"nombre": "Beta"}]}

    assert mapping.resolve(item, "x") == "Alpha"


def test_field_mapping_template_broken_dotted_path_raises():
    """Unlike an unknown flat placeholder (swallowed to ""), a dotted path
    that hits a missing key aborts the whole resolve() call — see
    FieldMapping's docstring."""
    mapping = FieldMapping(template="{estacion.nombre}")

    with pytest.raises(ValueError):
        mapping.resolve({"estacion": {"otro": "Alpha"}}, "x")


def test_field_mapping_field_date_format_supports_dotted_bare_placeholder():
    mapping = FieldMapping(template="{estacion.fecha}", field_date_format="%Y-%m-%d %H:%M:%S")

    result = mapping.resolve({"estacion": {"fecha": "2026-01-01 10:00:00"}}, "x")

    assert result == "2026-01-01T10:00:00"


def test_field_mapping_field_date_format_dotted_bare_placeholder_soft_none_when_base_missing():
    """The base name being entirely absent stays a soft None (this item
    just doesn't have that optional data), matching the pre-existing flat
    bare-field behavior — only a structural break past a present base key
    raises."""
    mapping = FieldMapping(template="{estacion.fecha}", field_date_format="%Y-%m-%d %H:%M:%S")

    assert mapping.resolve({}, "x") is None


def test_api_adapter_config_resolve_source_date_time_none_when_unconfigured():
    cfg = ApiAdapterConfig.from_dict({"url": "https://example.test/"})

    assert cfg.resolve_source_date_time({"Date": "2026-01-01"}) is None
