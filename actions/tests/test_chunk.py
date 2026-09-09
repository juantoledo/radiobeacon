from adapters.storage import get_connection, set_setting, set_source

from actions.chunk import ChunkAction, _effective_max_chars, _wrap_with_part_markers


def _insert_item(
    conn, source, item_id, extracted_contents, *,
    summary=None, extracted_title=None, url=None, item_type=None, subtype=None,
):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_contents, summary, extracted_title, "
        "url, type, subtype, fetched_at, rawdata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source, item_id, extracted_contents, summary, extracted_title,
            url, item_type, subtype, "2026-08-19T12:00:00", "{}",
        ),
    )
    conn.commit()


def _dispatched_event(source, item_id):
    return {
        "specversion": "1.0",
        "type": "cl.radiobeacon.item.dispatched",
        "source": "radiobeacon/log_handler",
        "data": {"source": source, "item_id": item_id},
    }


def _stored_chunks(conn, source, item_id):
    rows = conn.execute(
        "SELECT chunk_index, chunk_count, text FROM chunks "
        "WHERE source = ? AND item_id = ? ORDER BY chunk_index",
        (source, item_id),
    ).fetchall()
    return [{"chunk_index": r[0], "chunk_count": r[1], "text": r[2]} for r in rows]


def test_chunk_splits_extracted_contents_at_word_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "20")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "one two three four five six seven eight")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    assert len(stored) > 1
    for chunk in stored:
        assert len(chunk["text"]) <= 20
        assert not chunk["text"].startswith(" ")
        assert not chunk["text"].endswith(" ")


def test_chunk_respects_max_chars_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "1000")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "a short piece of text")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    assert len(stored) == 1
    assert stored[0]["text"] == "a short piece of text"


def test_chunk_skips_when_extracted_contents_is_null(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIONS_CHUNK_MAX_CHARS", raising=False)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", None)

    outputs = ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs == []
    assert _stored_chunks(conn, "senapred", "1") == []


def test_chunk_skips_when_item_not_found(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIONS_CHUNK_MAX_CHARS", raising=False)
    conn = get_connection(tmp_path / "radiobeacon.db")

    outputs = ChunkAction().run(_dispatched_event("senapred", "does-not-exist"), conn=conn)

    assert outputs == []
    assert _stored_chunks(conn, "senapred", "does-not-exist") == []


def test_chunk_skips_when_event_missing_source_or_item_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    outputs = ChunkAction().run({"data": {}}, conn=conn)

    assert outputs == []


def test_chunk_output_items_carry_source_and_item_id_for_reassembly(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "10")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "one two three four five")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    assert len(stored) > 1
    for i, chunk in enumerate(stored):
        assert chunk["chunk_index"] == i
        assert chunk["chunk_count"] == len(stored)


def test_chunk_returns_single_summary_item_not_one_per_chunk(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "10")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "one two three four five")

    outputs = ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert len(outputs) == 1
    assert outputs[0]["source"] == "senapred"
    assert outputs[0]["item_id"] == "1"
    assert outputs[0]["chunk_count"] == len(_stored_chunks(conn, "senapred", "1"))
    assert "chunk_index" not in outputs[0]
    assert "text" not in outputs[0]


def test_chunk_stores_all_chunks_before_returning(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "10")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "one two three four five")

    outputs = ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert len(_stored_chunks(conn, "senapred", "1")) == outputs[0]["chunk_count"]


def test_chunk_normalizes_literal_unicode_escapes(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "1000")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "informaci\\u00f3n meteorol\\u00f3gica")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    assert len(stored) == 1
    assert stored[0]["text"] == "información meteorológica"


def test_chunk_picks_up_settings_row_without_restart(tmp_path, monkeypatch):
    """ChunkAction re-resolves ACTIONS_CHUNK_MAX_CHARS via get_setting(conn=conn)
    on every run() call — a DB-stored override (set via the /config UI) takes
    effect on the very next message, no process restart needed."""
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "1000")  # would keep this a single chunk
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "10")  # DB override should win
    _insert_item(conn, "senapred", "1", "one two three four five")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    assert len(stored) > 1
    for chunk in stored:
        assert len(chunk["text"]) <= 10


def test_chunk_leaves_real_backslashes_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "1000")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "ruta C:\\datos\\alerta")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    assert stored[0]["text"] == "ruta C:\\datos\\alerta"


# --- summary-preferred content (actions.chunk now runs after actions.ai) ---


def test_chunk_prefers_summary_over_extracted_contents_when_both_present(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "1000")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "raw extracted contents", summary="a short AI summary")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    assert len(stored) == 1
    assert stored[0]["text"] == "a short AI summary"


def test_chunk_falls_back_to_extracted_contents_when_no_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "1000")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", "raw extracted contents", summary=None)

    ChunkAction().run(_dispatched_event("csn", "1"), conn=conn)
    stored = _stored_chunks(conn, "csn", "1")

    assert stored[0]["text"] == "raw extracted contents"


# --- dynamic AX.25-aware max_chars clamp ---


def test_effective_max_chars_unclamped_when_callsign_not_configured(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert _effective_max_chars(conn, 200) == 200


def test_effective_max_chars_unclamped_when_budget_is_larger_than_configured(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_CALLSIGN", "N0CALL-1")

    assert _effective_max_chars(conn, 50) == 50  # 50 chars fits comfortably under 256 bytes


def test_effective_max_chars_clamps_down_when_suffix_would_overflow(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_CALLSIGN", "N0CALL-1")
    set_setting(conn, "BEACON_FRAME_SUFFIX", "x" * 200)  # eats most of the 256-byte budget

    result = _effective_max_chars(conn, 200)

    assert result < 200
    assert result >= 1


def test_effective_max_chars_measures_rendered_date_length_not_raw_template(tmp_path):
    """BEACON_FRAME_SUFFIX is a str.format template -- a short raw
    template like " {date}" (7 chars) can render to something much
    longer (e.g. " 22-08-2026 14:30", 18 chars) once BEACON_DATE_FORMAT
    is applied. Measuring the raw template's length would silently
    under-clamp and reopen the overflow risk this clamp exists to
    prevent."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_CALLSIGN", "N0CALL-1")
    set_setting(conn, "BEACON_FRAME_DESTINATION", "WXALRT")
    set_setting(conn, "BEACON_DATE_FORMAT", "%d-%m-%Y %H:%M")
    # 256 - len("N0CALL-1>WXALRT:") == 240 bytes available with no suffix.
    # A raw " {date}" template is only 7 chars, but renders to 17 chars
    # (" 31-12-2026 23:59", the sample date chunk.py measures against) --
    # a naive raw-length measurement would (wrongly) leave
    # configured_max_chars=235 unclamped.
    set_setting(conn, "BEACON_FRAME_SUFFIX", " {date}")

    result = _effective_max_chars(conn, 235)

    assert result < 235  # must account for the RENDERED suffix length
    assert result == 256 - len("N0CALL-1>WXALRT:") - len(" 31-12-2026 23:59")


def test_chunk_uses_dynamically_clamped_max_chars(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "200")
    set_setting(conn, "BEACON_CALLSIGN", "N0CALL-1")
    set_setting(conn, "BEACON_FRAME_SUFFIX", "x" * 200)
    _insert_item(conn, "senapred", "1", "one two three four five six seven eight nine ten")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    for chunk in stored:
        assert len(chunk["text"]) < 200


def test_effective_max_chars_clamps_down_using_real_item_field_values(tmp_path):
    """BEACON_FRAME_PREFIX referencing {extracted_title} must account for
    THIS item's real (long) title, not treat item_fields as empty -- a
    long title can vary the byte overhead as much as a long suffix can."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_CALLSIGN", "N0CALL-1")
    set_setting(conn, "BEACON_FRAME_DESTINATION", "WXALRT")
    set_setting(conn, "BEACON_FRAME_PREFIX", "[{extracted_title}] ")
    long_title = "x" * 200

    result = _effective_max_chars(
        conn, 200, item_fields={
            "source": "senapred", "item_id": "1", "type": "", "subtype": "",
            "extracted_title": long_title, "url": "",
        },
    )

    assert result < 200
    assert result == 256 - len("N0CALL-1>WXALRT:") - len(f"[{long_title}] ")


def test_effective_max_chars_without_item_fields_treats_placeholders_as_empty(tmp_path):
    """The existing (pre-item-fields) call signature -- item_fields
    omitted entirely -- must keep behaving exactly as before: an
    item-field placeholder in the template renders as empty, not a
    crash."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_CALLSIGN", "N0CALL-1")
    set_setting(conn, "BEACON_FRAME_DESTINATION", "WXALRT")
    set_setting(conn, "BEACON_FRAME_PREFIX", "[{type}] " + "x" * 100)

    result = _effective_max_chars(conn, 200)

    # {type} renders as "" (empty item_fields), not a crash -- the clamp
    # still accounts for the rest of the literal prefix text.
    assert result == 256 - len("N0CALL-1>WXALRT:") - len("[] " + "x" * 100)


def test_effective_max_chars_falls_back_to_empty_on_invalid_placeholder(tmp_path, caplog):
    """A typo'd placeholder in BEACON_FRAME_PREFIX must not crash the
    clamp calculation (or, transitively, the whole chunk action) -- it
    logs an error and is treated as if the prefix were empty."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_CALLSIGN", "N0CALL-1")
    set_setting(conn, "BEACON_FRAME_DESTINATION", "WXALRT")
    set_setting(conn, "BEACON_FRAME_PREFIX", "[{typeo}] ")

    result = _effective_max_chars(conn, 200)

    assert result == 200  # empty prefix -- no clamping needed


def test_chunk_action_renders_item_field_placeholder_in_frame_prefix(tmp_path):
    """End-to-end: ChunkAction.run() itself passes real item_fields
    through to _effective_max_chars, so a long extracted_title actually
    shrinks the stored chunks, not just the isolated clamp function."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "200")
    set_setting(conn, "BEACON_CALLSIGN", "N0CALL-1")
    set_setting(conn, "BEACON_FRAME_DESTINATION", "WXALRT")
    set_setting(conn, "BEACON_FRAME_PREFIX", "[{extracted_title}] ")
    _insert_item(
        conn, "senapred", "1", "one two three four five six seven eight nine ten",
        extracted_title="x" * 200,
    )

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    for chunk in stored:
        assert len(chunk["text"]) < 56  # 256 - len("N0CALL-1>WXALRT:") - len("[" + "x"*200 + "] ")


def test_chunk_action_renders_source_name_placeholder_in_frame_prefix(tmp_path):
    """{source_name} comes from the `sources` table (seeded with csn/
    senapred, editable via data-adapters/sources.sh), not the item's own
    row -- ChunkAction.run() must still resolve and clamp against it like
    any other item_fields placeholder."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "200")
    set_setting(conn, "BEACON_CALLSIGN", "N0CALL-1")
    set_setting(conn, "BEACON_FRAME_DESTINATION", "WXALRT")
    set_setting(conn, "BEACON_FRAME_PREFIX", "[{source_name}] ")
    set_source(conn, "csn", "x" * 200)  # long enough to force clamping
    _insert_item(conn, "csn", "1", "one two three four five six seven eight nine ten")

    ChunkAction().run(_dispatched_event("csn", "1"), conn=conn)
    stored = _stored_chunks(conn, "csn", "1")

    assert stored
    for chunk in stored:
        assert len(chunk["text"]) < 56  # 256 - len("N0CALL-1>WXALRT:") - len("[" + "x"*200 + "] ")


# --- part markers (1/n, 2/n, ...) on multi-chunk content ---


def test_wrap_with_part_markers_no_marker_for_single_piece():
    pieces = _wrap_with_part_markers("a short piece of text", max_chars=1000)

    assert pieces == ["a short piece of text"]


def test_wrap_with_part_markers_suffixes_each_piece(tmp_path):
    text = "one two three four five six seven eight nine ten"
    pieces = _wrap_with_part_markers(text, max_chars=20)

    assert len(pieces) > 1
    total = len(pieces)
    for i, piece in enumerate(pieces, start=1):
        assert piece.endswith(f" {i}/{total}")


def test_wrap_with_part_markers_respects_max_chars_budget():
    text = "one two three four five six seven eight nine ten eleven twelve"
    max_chars = 20

    pieces = _wrap_with_part_markers(text, max_chars)

    for piece in pieces:
        assert len(piece) <= max_chars


def test_wrap_with_part_markers_content_recoverable_after_stripping_marker():
    text = "one two three four five six seven eight nine ten"
    pieces = _wrap_with_part_markers(text, max_chars=20)

    stripped = " ".join(piece.rsplit(" ", 1)[0] for piece in pieces)
    assert stripped == text  # word-boundary splits, rejoined with a single space


def test_chunk_stores_part_markers_in_stored_text_for_multi_chunk_item(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "20")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "one two three four five six seven eight nine ten")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    assert len(stored) > 1
    total = len(stored)
    for i, chunk in enumerate(stored, start=1):
        assert chunk["text"].endswith(f" {i}/{total}")


def test_chunk_does_not_add_marker_for_single_chunk_item(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "1000")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "a short piece of text")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    assert len(stored) == 1
    assert stored[0]["text"] == "a short piece of text"  # no "1/1 " marker
