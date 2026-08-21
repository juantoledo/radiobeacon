from adapters.storage import get_connection

from actions.chunk import ChunkAction


def _insert_item(conn, source, item_id, extracted_contents):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_contents, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, item_id, extracted_contents, "2026-08-19T12:00:00", "{}"),
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


def test_chunk_leaves_real_backslashes_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "1000")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "ruta C:\\datos\\alerta")

    ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)
    stored = _stored_chunks(conn, "senapred", "1")

    assert stored[0]["text"] == "ruta C:\\datos\\alerta"
