from adapters.ax25 import AX25_HARD_LIMIT_BYTES, max_frame_content_bytes


def test_max_frame_content_bytes_basic_ascii_overhead():
    # The frame prefix is "<callsign>><destination>:" — one ASCII byte per char.
    available = max_frame_content_bytes(callsign="N0CALL-1", destination="WXALRT")

    assert available == AX25_HARD_LIMIT_BYTES - len("N0CALL-1>WXALRT:")


def test_max_frame_content_bytes_accounts_for_prefix_and_suffix():
    baseline = max_frame_content_bytes(callsign="N0CALL-1", destination="WXALRT")
    with_wrap = max_frame_content_bytes(
        callsign="N0CALL-1", destination="WXALRT", prefix=">> ", suffix=" [EXPERIMENTAL]"
    )

    assert with_wrap == baseline - len(">> ") - len(" [EXPERIMENTAL]")


def test_max_frame_content_bytes_uses_utf8_byte_length_not_char_count():
    # Each 'ó' is 2 bytes in UTF-8 but 1 character.
    baseline = max_frame_content_bytes(callsign="X", destination="Y")
    with_accented_suffix = max_frame_content_bytes(callsign="X", destination="Y", suffix="óóóó")

    assert with_accented_suffix == baseline - 8  # 4 chars * 2 bytes each


def test_max_frame_content_bytes_can_go_negative_when_overhead_exceeds_limit():
    huge_suffix = "x" * 300

    available = max_frame_content_bytes(callsign="X", destination="Y", suffix=huge_suffix)

    assert available < 0
