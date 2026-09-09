import logging

import pytest

from adapters.templating import dotted_placeholder_paths, resolve_dotted_path, safe_format


def test_safe_format_renders_known_placeholders():
    result = safe_format("[{type}] {text} ({date})", "TEST_SETTING", type="alerta", text="hola", date="22-08-2026")

    assert result == "[alerta] hola (22-08-2026)"


def test_safe_format_empty_template_returns_empty_string():
    assert safe_format("", "TEST_SETTING", date="22-08-2026") == ""


def test_safe_format_falls_back_to_empty_string_on_unknown_placeholder(caplog):
    with caplog.at_level(logging.ERROR):
        result = safe_format("{typo}", "BEACON_FRAME_PREFIX", date="22-08-2026")

    assert result == ""
    assert "BEACON_FRAME_PREFIX" in caplog.text
    assert "{typo}" in caplog.text


def test_safe_format_falls_back_to_empty_string_on_index_error(caplog):
    with caplog.at_level(logging.ERROR):
        result = safe_format("{0}", "BEACON_VOICE_SUFFIX")

    assert result == ""
    assert "BEACON_VOICE_SUFFIX" in caplog.text


def test_safe_format_passes_through_literal_text_unchanged():
    assert safe_format("[EXPERIMENTAL]", "BEACON_FRAME_SUFFIX") == "[EXPERIMENTAL]"


def test_safe_format_refuses_index_traversal(caplog):
    with caplog.at_level(logging.ERROR):
        index = safe_format("{x[0]}", "BEACON_VOICE_TEMPLATE", x="hi")

    assert index == ""
    assert "BEACON_VOICE_TEMPLATE" in caplog.text


def test_safe_format_dotted_attribute_walk_still_falls_back_to_empty_string(caplog):
    """Dots are now allowed in a placeholder, but only ever resolved via
    dict lookups (see resolve_dotted_path) -- never getattr -- so this
    still safely falls back to "", just because "hi" isn't a dict rather
    than because dots are forbidden outright."""
    with caplog.at_level(logging.ERROR):
        result = safe_format(
            "{x.__class__.__init__.__globals__}", "ACTIONS_AI_PROMPT", x="hi"
        )

    assert result == ""
    assert "ACTIONS_AI_PROMPT" in caplog.text


def test_safe_format_supports_dotted_nested_dict_access():
    result = safe_format("{station.name}", "TEST_SETTING", station={"name": "Alpha"})

    assert result == "Alpha"


def test_safe_format_dotted_access_unwraps_list_encountered_mid_path():
    result = safe_format(
        "{data.station.name}",
        "TEST_SETTING",
        data={"station": [{"name": "Alpha"}, {"name": "Beta"}]},
    )

    assert result == "Alpha"


def test_safe_format_dotted_access_unwraps_list_as_the_final_value():
    result = safe_format("{data.tags}", "TEST_SETTING", data={"tags": ["first", "second"]})

    assert result == "first"


def test_safe_format_dotted_access_falls_back_to_empty_string_on_missing_key(caplog):
    with caplog.at_level(logging.ERROR):
        result = safe_format("{station.missing}", "TEST_SETTING", station={"name": "Alpha"})

    assert result == ""
    assert "TEST_SETTING" in caplog.text


def test_resolve_dotted_path_unwraps_empty_list_to_none_then_raises():
    with pytest.raises(ValueError):
        resolve_dotted_path({"station": []}, "station.name")


def test_resolve_dotted_path_tolerates_a_present_null_leaf():
    assert resolve_dotted_path({"station": {"name": None}}, "station.name") is None


def test_dotted_placeholder_paths_finds_only_dotted_placeholders():
    pairs = dotted_placeholder_paths("Sismo {station.name} M{Magnitud} - {a.b.c}")

    assert pairs == [("station", "name"), ("a", "b.c")]
