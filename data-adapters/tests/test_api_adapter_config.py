import uuid

from adapters.api_adapter import ApiAdapterConfig, TransmitPolicyRule, FieldMapping


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


def test_transmit_policy_rule_resolves_above_and_below_threshold():
    rule = TransmitPolicyRule(field="magnitude", threshold=4.5)

    assert rule.resolve({"magnitude": "5.0"}) == "urgent"
    assert rule.resolve({"magnitude": "3.0"}) == "informational"


def test_transmit_policy_rule_returns_none_when_field_missing():
    rule = TransmitPolicyRule(field="magnitude", threshold=4.5)

    assert rule.resolve({}) is None


def test_transmit_policy_rule_supports_other_operators():
    rule = TransmitPolicyRule(field="x", operator="<", threshold=10, if_true="a", if_false="b")

    assert rule.resolve({"x": "5"}) == "a"
    assert rule.resolve({"x": "15"}) == "b"


def test_api_adapter_config_from_dict_minimal():
    cfg = ApiAdapterConfig.from_dict({"url": "https://example.test/"})

    assert cfg.url == "https://example.test/"
    assert cfg.method == "GET"
    assert cfg.headers == {}
    assert cfg.query_params == {}
    assert cfg.items_path == ""
    assert cfg.mapping == {}
    assert cfg.transmit_policy_rule is None


def test_api_adapter_config_from_dict_full():
    cfg = ApiAdapterConfig.from_dict(
        {
            "url": "https://example.test/",
            "method": "POST",
            "headers": {"Accept": "application/json"},
            "query_params": {"key": "value"},
            "body": "{}",
            "items_path": "result.items",
            "mapping": {"id": {"field": "Id"}, "title": {"value": "fixed"}},
            "date_field": "Date",
            "date_format": "%Y-%m-%d",
            "source_timezone": "America/Santiago",
            "transmit_policy_rule": {"field": "score", "threshold": 5},
        }
    )

    assert cfg.method == "POST"
    assert cfg.headers == {"Accept": "application/json"}
    assert cfg.query_params == {"key": "value"}
    assert cfg.body == "{}"
    assert cfg.items_path == "result.items"
    assert cfg.mapping_for("id") == FieldMapping(template="{Id}")
    assert cfg.mapping_for("title") == FieldMapping(template="fixed")
    assert cfg.mapping_for("contents") == FieldMapping()  # unconfigured -> empty, not KeyError
    assert cfg.transmit_policy_rule == TransmitPolicyRule(field="score", threshold=5)


def test_api_adapter_config_resolve_source_date_time_none_when_unconfigured():
    cfg = ApiAdapterConfig.from_dict({"url": "https://example.test/"})

    assert cfg.resolve_source_date_time({"Date": "2026-01-01"}) is None


def test_api_adapter_config_resolve_transmit_policy_none_when_unconfigured():
    cfg = ApiAdapterConfig.from_dict({"url": "https://example.test/"})

    assert cfg.resolve_transmit_policy({"score": "10"}) is None


def test_api_adapter_config_from_dict_honors_legacy_dispatch_policy_rule_key():
    """A config row written before the rename still carries
    dispatch_policy_rule — from_dict falls back to it."""
    cfg = ApiAdapterConfig.from_dict(
        {
            "url": "https://example.test/",
            "dispatch_policy_rule": {"field": "score", "threshold": 5},
        }
    )

    assert cfg.transmit_policy_rule == TransmitPolicyRule(field="score", threshold=5)
