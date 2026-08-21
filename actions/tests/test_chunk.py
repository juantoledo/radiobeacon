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


def test_chunk_splits_extracted_contents_at_word_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "20")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "one two three four five six seven eight")

    outputs = ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert len(outputs) > 1
    for chunk in outputs:
        assert len(chunk["text"]) <= 20
        assert not chunk["text"].startswith(" ")
        assert not chunk["text"].endswith(" ")


def test_chunk_respects_max_chars_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "1000")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "a short piece of text")

    outputs = ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert len(outputs) == 1
    assert outputs[0]["text"] == "a short piece of text"


def test_chunk_skips_when_extracted_contents_is_null(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIONS_CHUNK_MAX_CHARS", raising=False)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", None)

    outputs = ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs == []


def test_chunk_skips_when_item_not_found(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIONS_CHUNK_MAX_CHARS", raising=False)
    conn = get_connection(tmp_path / "radiobeacon.db")

    outputs = ChunkAction().run(_dispatched_event("senapred", "does-not-exist"), conn=conn)

    assert outputs == []


def test_chunk_skips_when_event_missing_source_or_item_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    outputs = ChunkAction().run({"data": {}}, conn=conn)

    assert outputs == []


def test_chunk_output_items_carry_source_and_item_id_for_reassembly(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "10")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "one two three four five")

    outputs = ChunkAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert len(outputs) > 1
    for i, chunk in enumerate(outputs):
        assert chunk["source"] == "senapred"
        assert chunk["item_id"] == "1"
        assert chunk["chunk_index"] == i
        assert chunk["chunk_count"] == len(outputs)
