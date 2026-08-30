from adapters.storage import record_audit_event
from adapters.transmit_policy import set_policy

from ui import queries


def _insert_item(
    conn,
    source,
    item_id,
    *,
    title="Title",
    contents="Contents",
    url="http://example.test",
    event_key=None,
    type_=None,
    transmit_policy=None,
    source_date_time=None,
):
    conn.execute(
        "INSERT INTO items "
        "(source, item_id, extracted_title, extracted_contents, url, "
        "event_key, type, transmit_policy, source_date_time, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), '{}')",
        (
            source,
            item_id,
            title,
            contents,
            url,
            event_key,
            type_,
            transmit_policy,
            source_date_time,
        ),
    )
    conn.commit()


def _insert_chunk(conn, source, item_id, chunk_index, chunk_count, text):
    conn.execute(
        "INSERT INTO chunks (source, item_id, chunk_index, chunk_count, text) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, item_id, chunk_index, chunk_count, text),
    )
    conn.commit()


def test_list_items_no_filters_returns_all_newest_first(conn):
    _insert_item(conn, "csn", "1")
    _insert_item(conn, "csn", "2")

    rows, total = queries.list_items(conn)

    assert total == 2
    assert [row["item_id"] for row in rows] == ["2", "1"]


def test_list_items_orders_by_source_date_time_desc_regardless_of_insertion_order(conn):
    # Inserted oldest-event-first, on purpose — the listing must still
    # come back newest-event-first, i.e. sorted by source_date_time, not
    # by insertion/rowid order.
    _insert_item(conn, "csn", "old", source_date_time="2026-01-01T00:00:00+00:00")
    _insert_item(conn, "csn", "newest", source_date_time="2026-03-01T00:00:00+00:00")
    _insert_item(conn, "csn", "mid", source_date_time="2026-02-01T00:00:00+00:00")

    rows, _ = queries.list_items(conn)

    assert [row["item_id"] for row in rows] == ["newest", "mid", "old"]


def test_recent_items_orders_by_source_date_time_desc(conn):
    _insert_item(conn, "csn", "old", source_date_time="2026-01-01T00:00:00+00:00")
    _insert_item(conn, "csn", "newest", source_date_time="2026-03-01T00:00:00+00:00")

    rows = queries.recent_items(conn)

    assert [row["item_id"] for row in rows] == ["newest", "old"]


def test_list_items_filters_by_source(conn):
    _insert_item(conn, "csn", "1")
    _insert_item(conn, "senapred", "1")

    rows, total = queries.list_items(conn, source="csn")

    assert total == 1
    assert rows[0]["source"] == "csn"


def test_list_items_filters_by_type_and_transmit_policy_combined(conn):
    _insert_item(conn, "csn", "1", type_="Alerta", transmit_policy="urgent")
    _insert_item(conn, "csn", "2", type_="Alerta", transmit_policy="informational")
    _insert_item(conn, "csn", "3", type_="Evento", transmit_policy="urgent")

    rows, total = queries.list_items(conn, type_="Alerta", transmit_policy="urgent")

    assert total == 1
    assert rows[0]["item_id"] == "1"


def test_list_items_filters_by_event_key(conn):
    _insert_item(conn, "senapred", "1", event_key="ek-1")
    _insert_item(conn, "senapred", "2", event_key="ek-2")

    rows, total = queries.list_items(conn, event_key="ek-1")

    assert total == 1
    assert rows[0]["item_id"] == "1"


def test_list_items_free_text_search_matches_title_contents_and_url(conn):
    _insert_item(conn, "csn", "1", title="Terremoto en Chile")
    _insert_item(conn, "csn", "2", contents="mentions chile in body")
    _insert_item(conn, "csn", "3", url="http://example.test/chile-report")
    _insert_item(conn, "csn", "4", title="unrelated", contents="nothing", url="http://x.test")

    rows, total = queries.list_items(conn, q="chile")

    assert total == 3
    assert {row["item_id"] for row in rows} == {"1", "2", "3"}


def test_list_items_pagination(conn):
    for i in range(5):
        _insert_item(conn, "csn", str(i))

    page1, total = queries.list_items(conn, limit=2, offset=0)
    page2, _ = queries.list_items(conn, limit=2, offset=2)

    assert total == 5
    assert len(page1) == 2
    assert len(page2) == 2
    assert {row["item_id"] for row in page1}.isdisjoint({row["item_id"] for row in page2})


def test_get_item_returns_row(conn):
    _insert_item(conn, "csn", "1", title="Hello")

    row = queries.get_item(conn, "csn", "1")

    assert row is not None
    assert row["extracted_title"] == "Hello"


def test_get_item_returns_none_for_unknown(conn):
    assert queries.get_item(conn, "csn", "does-not-exist") is None


def test_list_item_chunks_ordered_by_chunk_index(conn):
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 1, 2, "second")
    _insert_chunk(conn, "csn", "1", 0, 2, "first")

    chunks = queries.list_item_chunks(conn, "csn", "1")

    assert [c["text"] for c in chunks] == ["first", "second"]


def test_list_audit_log_for_item_filters_by_source_and_item_id(conn):
    _insert_item(conn, "csn", "1")
    record_audit_event(conn, event_type="item.stored", actor="test", source="csn", item_id="1")
    record_audit_event(conn, event_type="item.stored", actor="test", source="csn", item_id="2")

    rows = queries.list_audit_log_for_item(conn, "csn", "1")

    assert len(rows) == 1
    assert rows[0]["item_id"] == "1"


def test_list_audit_log_filters_by_event_type(conn):
    record_audit_event(conn, event_type="item.stored", actor="test", source="csn", item_id="1")
    record_audit_event(conn, event_type="item.dispatched", actor="test", source="csn", item_id="1")

    rows, total = queries.list_audit_log(conn, event_type="item.dispatched")

    assert total == 1
    assert rows[0]["event_type"] == "item.dispatched"


def test_list_audit_log_pagination(conn):
    for i in range(5):
        record_audit_event(conn, event_type="item.stored", actor="test", source="csn", item_id=str(i))

    page1, total = queries.list_audit_log(conn, limit=2, offset=0)

    assert total == 5
    assert len(page1) == 2


def test_dashboard_counts(conn):
    _insert_item(conn, "csn", "1")
    _insert_item(conn, "csn", "2")
    record_audit_event(conn, event_type="item.stored", actor="test", source="csn", item_id="1")

    counts = queries.dashboard_counts(conn)

    assert counts["total_items"] == 2
    assert counts["items_last_24h"] == 2
    assert counts["total_audit_events"] == 1
    assert counts["in_flight_dispatches"] == 0
    # _ensure_tables seeds the two default policies (urgent, informational).
    assert counts["total_policies"] == 2


def test_recent_items_orders_newest_first_and_respects_limit(conn):
    for i in range(3):
        _insert_item(conn, "csn", str(i))

    rows = queries.recent_items(conn, limit=2)

    assert [r["item_id"] for r in rows] == ["2", "1"]


def test_recent_audit_events_orders_newest_first(conn):
    record_audit_event(conn, event_type="item.stored", actor="test", source="csn", item_id="1")
    record_audit_event(conn, event_type="item.dispatched", actor="test", source="csn", item_id="1")

    rows = queries.recent_audit_events(conn, limit=1)

    assert rows[0]["event_type"] == "item.dispatched"


def test_distinct_sources_and_types(conn):
    _insert_item(conn, "csn", "1", type_="Alerta")
    _insert_item(conn, "senapred", "1", type_="Evento")
    _insert_item(conn, "senapred", "2", type_="Evento")

    assert queries.distinct_sources(conn) == ["csn", "senapred"]
    assert queries.distinct_types(conn) == ["Alerta", "Evento"]


def test_get_policy_row_returns_full_row_with_description(conn):
    set_policy(conn, "custom", 3, 30, "a custom policy")

    row = queries.get_policy_row(conn, "custom")

    assert row == ("custom", 3, 30, "a custom policy")


def test_get_policy_row_returns_none_for_unknown(conn):
    assert queries.get_policy_row(conn, "does-not-exist") is None
