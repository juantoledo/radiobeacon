import pytest

from beacon.formatters import FrameTooLongError, format_frame, format_voice


def test_format_frame_builds_tnc2_line():
    result = format_frame("hola mundo", callsign="CD3DXZ-1", destination="WXALRT")

    assert result.tnc2 == "CD3DXZ-1>WXALRT:hola mundo"
    assert result.content == "hola mundo"
    assert result.byte_length == len(result.tnc2.encode("utf-8"))


def test_format_frame_no_prefix_suffix_by_default_is_backward_compatible():
    result = format_frame("hola mundo", callsign="CD3DXZ-1", destination="WXALRT")

    assert result.content == "hola mundo"


def test_format_frame_applies_prefix_and_suffix_to_content_and_tnc2():
    result = format_frame(
        "hola mundo", callsign="CD3DXZ-1", destination="WXALRT",
        prefix=">> ", suffix=" [EXPERIMENTAL]",
    )

    assert result.content == ">> hola mundo [EXPERIMENTAL]"
    assert result.tnc2 == "CD3DXZ-1>WXALRT:>> hola mundo [EXPERIMENTAL]"


def test_format_frame_prefix_suffix_pushing_over_limit_still_raises():
    prefix_len = len("CD3DXZ-1>WXALRT:")
    body = "x" * (256 - prefix_len)  # exactly at the limit with no suffix

    with pytest.raises(FrameTooLongError):
        format_frame(body, callsign="CD3DXZ-1", destination="WXALRT", suffix=" [EXPERIMENTAL]")


def test_format_frame_raises_when_over_hard_byte_limit():
    long_text = "x" * 300  # well past the 256-byte AX.25 UI frame limit

    with pytest.raises(FrameTooLongError):
        format_frame(long_text, callsign="CD3DXZ-1", destination="WXALRT")


def test_format_frame_accepts_content_right_at_the_limit():
    # "CD3DXZ-1>WXALRT:" is 17 ASCII bytes -- pad to exactly 256 total.
    prefix_len = len("CD3DXZ-1>WXALRT:")
    body = "x" * (256 - prefix_len)

    result = format_frame(body, callsign="CD3DXZ-1", destination="WXALRT")

    assert result.byte_length == 256


def test_format_frame_renders_date_placeholder_in_suffix():
    result = format_frame(
        "hola mundo", callsign="CD3DXZ-1", destination="WXALRT",
        suffix=" {date}", date="22-08-2026 14:30",
    )

    assert result.content == "hola mundo 22-08-2026 14:30"


def test_format_frame_literal_prefix_suffix_unaffected_by_date_param():
    """A plain literal with no {date} in it (today's already-deployed
    style) passes through .format() unchanged regardless of what `date`
    is passed."""
    result = format_frame(
        "hola mundo", callsign="CD3DXZ-1", destination="WXALRT",
        suffix=" [EXPERIMENTAL]", date="22-08-2026 14:30",
    )

    assert result.content == "hola mundo [EXPERIMENTAL]"


def test_format_frame_date_placeholder_pushing_over_limit_still_raises():
    prefix_len = len("CD3DXZ-1>WXALRT:")
    body = "x" * (256 - prefix_len)  # exactly at the limit with no suffix

    with pytest.raises(FrameTooLongError):
        format_frame(body, callsign="CD3DXZ-1", destination="WXALRT", suffix=" {date}", date="22-08-2026 14:30")


def test_format_frame_counts_utf8_bytes_not_chars_for_accented_text():
    """A chunk within ACTIONS_CHUNK_MAX_CHARS chars can still push the
    assembled line over the AX.25 byte limit once multi-byte accented
    characters are counted — this is exactly the scenario the hard byte
    ceiling exists to catch, distinct from ACTIONS_CHUNK_MAX_CHARS's own
    char-counting."""
    prefix_len = len("CD3DXZ-1>WXALRT:")
    # Each 'ó' is 2 bytes in UTF-8 but 1 character -- construct a body
    # that's within a plausible char count but over the byte limit.
    body = "ó" * (256 - prefix_len)  # 1 char short of the byte limit in bytes... see below
    # body has (256 - prefix_len) chars, each 2 bytes => well over 256 bytes total
    with pytest.raises(FrameTooLongError):
        format_frame(body, callsign="CD3DXZ-1", destination="WXALRT")


def test_format_voice_prefixes_callsign_via_template():
    result = format_voice(
        "un resumen corto", callsign="CD3DXZ-1", template="{callsign}. {text}", max_chars=500
    )

    assert result.text == "CD3DXZ-1. un resumen corto"
    assert result.truncated is False


def test_format_voice_no_op_when_under_max_chars():
    text = "texto breve"
    result = format_voice(text, callsign="CD3DXZ-1", template="{text}", max_chars=500)

    assert result.text == text
    assert result.truncated is False


def test_format_voice_truncates_word_boundary_safe_when_over_max_chars():
    text = "one two three four five six seven eight nine ten"
    result = format_voice(text, callsign="X", template="{text}", max_chars=20)

    assert result.truncated is True
    assert len(result.text) <= 20
    assert not result.text.endswith(" ")
    # word-boundary-safe: every character in the result is a prefix of a
    # real word from the original text, never a mid-word cut
    assert text.startswith(result.text.rstrip())


def test_format_voice_renders_date_placeholder():
    result = format_voice(
        "un resumen corto", callsign="CD3DXZ-1", template="{callsign}. {text}. {date}",
        max_chars=500, date="22-08-2026 14:30",
    )

    assert result.text == "CD3DXZ-1. un resumen corto. 22-08-2026 14:30"


def test_format_voice_date_defaults_to_blank():
    result = format_voice(
        "un resumen corto", callsign="CD3DXZ-1", template="{callsign}. {text}. {date}", max_chars=500
    )

    assert result.text == "CD3DXZ-1. un resumen corto. "


# --- item_fields placeholders (source, item_id, type, subtype, extracted_title, url) ---


def test_format_frame_renders_item_field_placeholder_in_prefix():
    result = format_frame(
        "hola mundo", callsign="CD3DXZ-1", destination="WXALRT",
        prefix="[{type}] ", type="alerta", subtype="", extracted_title="", url="", source="", item_id="",
    )

    assert result.content == "[alerta] hola mundo"


def test_format_frame_falls_back_to_empty_on_invalid_placeholder():
    """A typo'd placeholder must not crash format_frame -- the TDMA loop
    has no per-tick catch-all around this call."""
    result = format_frame(
        "hola mundo", callsign="CD3DXZ-1", destination="WXALRT", prefix="[{typeo}] ", type="alerta",
    )

    assert result.content == "hola mundo"  # prefix fell back to ""


def test_format_voice_wraps_text_with_prefix_and_suffix():
    result = format_voice(
        "un resumen corto", callsign="CD3DXZ-1", template="{text}", max_chars=500,
        prefix=">> ", suffix=" <<",
    )

    assert result.text == ">> un resumen corto <<"


def test_format_voice_prefix_suffix_added_outside_max_chars_budget():
    """Mirrors format_frame: prefix/suffix wrap the already-truncated
    text, they don't count against max_chars themselves."""
    text = "one two three four five six seven eight nine ten"
    result = format_voice(
        text, callsign="X", template="{text}", max_chars=20,
        prefix="PREFIX ", suffix=" SUFFIX",
    )

    assert result.truncated is True
    assert result.text.startswith("PREFIX ")
    assert result.text.endswith(" SUFFIX")


def test_format_voice_renders_item_field_placeholder_in_template_and_prefix():
    result = format_voice(
        "un resumen corto", callsign="CD3DXZ-1", template="{callsign}. [{type}] {text}",
        max_chars=500, prefix="({extracted_title}) ",
        type="alerta", subtype="", extracted_title="Alerta importante", url="", source="", item_id="",
    )

    assert result.text == "CD3DXZ-1. [alerta] (Alerta importante) un resumen corto"


def test_format_voice_falls_back_to_empty_on_invalid_placeholder():
    """A typo'd placeholder in prefix/suffix/template must not crash
    format_voice -- the TDMA loop has no per-tick catch-all around this
    call."""
    result = format_voice(
        "un resumen corto", callsign="CD3DXZ-1", template="{callsign}. {text}",
        max_chars=500, prefix="[{typeo}] ",
    )

    assert result.text == "CD3DXZ-1. un resumen corto"  # prefix fell back to ""
