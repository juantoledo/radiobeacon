from datetime import datetime, timezone

from adapters.storage import get_connection, set_adapter_instance

from beacon.content import (
    resolve_frame_text,
    resolve_item_fields,
    resolve_source_date_time,
    resolve_voice_replacements,
    resolve_voice_text,
)


def _insert_item(
    conn, source, item_id, *,
    extracted_contents=None, summary=None, source_date_time=None,
    extracted_title=None, url=None, item_type=None, subtype=None,
):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_contents, summary, source_date_time, "
        "extracted_title, url, type, subtype, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), '{}')",
        (source, item_id, extracted_contents, summary, source_date_time, extracted_title, url, item_type, subtype),
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


def test_resolve_voice_text_returns_none_when_summary_is_null_even_with_extracted_contents(tmp_path):
    """actions.ai now guarantees summary is populated (real or an
    identity copy of extracted_contents) whenever extracted_contents
    exists, so resolve_voice_text no longer falls back on its own --
    NULL summary means NULL, even if extracted_contents is set (e.g. an
    item predating that guarantee, never reprocessed)."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", extracted_contents="Sismo de magnitud 4.2.", summary=None)

    assert resolve_voice_text(conn, "csn", "1") is None


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


def test_resolve_item_fields_returns_all_present_fields(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(
        conn, "senapred", "1",
        extracted_title="Alerta importante", url="https://example.com/a",
        item_type="alerta", subtype="meteorologica",
    )

    assert resolve_item_fields(conn, "senapred", "1") == {
        "type": "alerta", "subtype": "meteorologica",
        "extracted_title": "Alerta importante", "url": "https://example.com/a",
        "source_name": "Senapred", "source_url": "https://senapred.cl/",
    }


def test_resolve_item_fields_coerces_null_columns_to_empty_strings(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")

    assert resolve_item_fields(conn, "csn", "1") == {
        "type": "", "subtype": "", "extracted_title": "", "url": "",
        "source_name": "Centro Sismológico Nacional", "source_url": "https://www.sismologia.cl/",
    }


def test_resolve_item_fields_all_empty_when_item_gone(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert resolve_item_fields(conn, "csn", "does-not-exist") == {
        "type": "", "subtype": "", "extracted_title": "", "url": "",
        "source_name": "Centro Sismológico Nacional", "source_url": "https://www.sismologia.cl/",
    }


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


def test_resolve_voice_replacements_returns_saved_map(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(
        conn, "csn", "api",
        {"url": "http://x", "voice_replacements": {"km": "kilómetros", "NE": "noreste"}},
    )

    assert resolve_voice_replacements(conn, "csn") == {"km": "kilómetros", "NE": "noreste"}


def test_resolve_voice_replacements_empty_when_key_absent(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(conn, "csn", "api", {"url": "http://x"})

    assert resolve_voice_replacements(conn, "csn") == {}


def test_resolve_voice_replacements_empty_when_no_adapter_row(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert resolve_voice_replacements(conn, "nope") == {}


def test_resolve_voice_replacements_empty_when_value_not_a_dict(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(
        conn, "csn", "api", {"url": "http://x", "voice_replacements": ["km", "kilómetros"]}
    )

    assert resolve_voice_replacements(conn, "csn") == {}
