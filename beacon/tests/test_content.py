from datetime import datetime, timezone

from adapters.storage import get_connection

from beacon.content import resolve_frame_text, resolve_source_date_time, resolve_voice_text


def _insert_item(conn, source, item_id, *, extracted_contents=None, summary=None, source_date_time=None):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_contents, summary, source_date_time, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), '{}')",
        (source, item_id, extracted_contents, summary, source_date_time),
    )
    conn.commit()


def _insert_chunk(conn, source, item_id, chunk_index, text, chunk_count=1):
    conn.execute(
        "INSERT INTO chunks (source, item_id, chunk_index, chunk_count, text) VALUES (?, ?, ?, ?, ?)",
        (source, item_id, chunk_index, chunk_count, text),
    )
    conn.commit()


def test_resolve_voice_text_prefers_summary_when_present(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", extracted_contents="raw contents", summary="AI summary")

    assert resolve_voice_text(conn, "senapred", "1") == "AI summary"


def test_resolve_voice_text_falls_back_to_extracted_contents_when_no_summary(tmp_path):
    """The CSN scenario: AI never summarizes structurally-short content,
    so summary stays NULL forever -- voice must still work."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", extracted_contents="Sismo de magnitud 4.2.", summary=None)

    assert resolve_voice_text(conn, "csn", "1") == "Sismo de magnitud 4.2."


def test_resolve_voice_text_returns_none_when_item_gone(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert resolve_voice_text(conn, "csn", "does-not-exist") is None


def test_resolve_voice_text_returns_none_when_no_content_either_way(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", extracted_contents=None, summary=None)

    assert resolve_voice_text(conn, "csn", "1") is None


def test_resolve_frame_text_reads_the_specific_chunk(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "first chunk", chunk_count=2)
    _insert_chunk(conn, "csn", "1", 1, "second chunk", chunk_count=2)

    assert resolve_frame_text(conn, "csn", "1", 0) == "first chunk"
    assert resolve_frame_text(conn, "csn", "1", 1) == "second chunk"


def test_resolve_frame_text_returns_none_when_chunk_gone(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert resolve_frame_text(conn, "csn", "does-not-exist", 0) is None


def test_resolve_source_date_time_parses_stored_iso_string(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    dt = datetime(2026, 8, 22, 14, 30, tzinfo=timezone.utc)
    _insert_item(conn, "senapred", "1", source_date_time=dt.isoformat())

    result = resolve_source_date_time(conn, "senapred", "1")

    assert result == dt
    assert result.tzinfo is not None


def test_resolve_source_date_time_returns_none_when_item_gone(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert resolve_source_date_time(conn, "senapred", "does-not-exist") is None


def test_resolve_source_date_time_returns_none_when_column_null(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", source_date_time=None)

    assert resolve_source_date_time(conn, "senapred", "1") is None
