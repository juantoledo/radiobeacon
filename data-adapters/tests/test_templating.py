import logging

from adapters.templating import safe_format


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


def test_safe_format_refuses_attribute_and_index_traversal(caplog):
    with caplog.at_level(logging.ERROR):
        attr = safe_format(
            "{x.__class__.__init__.__globals__}", "ACTIONS_AI_PROMPT", x="hi"
        )
        index = safe_format("{x[0]}", "BEACON_VOICE_TEMPLATE", x="hi")

    assert attr == ""
    assert index == ""
    assert "ACTIONS_AI_PROMPT" in caplog.text
