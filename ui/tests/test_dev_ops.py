import json

import pytest

from ui import dev_ops


def _insert_item(conn, source, item_id, **overrides):
    fields = {
        "extracted_title": "Title",
        "extracted_contents": "Contents",
        "summary": None,
        "url": "http://example.test",
        "event_key": None,
        "type": None,
        "subtype": None,
        "policy": "informational",
        "source_date_time": None,
    }
    fields.update(overrides)
    conn.execute(
        "INSERT INTO items "
        "(source, item_id, extracted_title, extracted_contents, summary, url, "
        "event_key, type, subtype, policy, source_date_time, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), '{}')",
        (
            source,
            item_id,
            fields["extracted_title"],
            fields["extracted_contents"],
            fields["summary"],
            fields["url"],
            fields["event_key"],
            fields["type"],
            fields["subtype"],
            fields["policy"],
            fields["source_date_time"],
        ),
    )
    conn.commit()


def _create_kwargs(**overrides):
    kwargs = dict(
        source="csn",
        item_id="1",
        extracted_title="Hand-made",
        extracted_contents="Body",
        summary=None,
        url="http://example.test",
        event_key=None,
        type_=None,
        subtype=None,
        policy="informational",
        source_date_time=None,
    )
    kwargs.update(overrides)
    return kwargs


def test_create_item_inserts_row(conn):
    dev_ops.create_item(conn, **_create_kwargs())

    row = conn.execute(
        "SELECT extracted_title, policy FROM items WHERE source='csn' AND item_id='1'"
    ).fetchone()
    assert tuple(row) == ("Hand-made", "informational")


def test_create_item_sets_fetched_at_and_marker_rawdata(conn):
    dev_ops.create_item(conn, **_create_kwargs())

    row = conn.execute(
        "SELECT fetched_at, rawdata FROM items WHERE source='csn' AND item_id='1'"
    ).fetchone()
    assert row["fetched_at"]
    assert json.loads(row["rawdata"]) == {"_created_via": "ui.dev"}


def test_create_item_raises_integrity_error_on_duplicate_pk(conn):
    dev_ops.create_item(conn, **_create_kwargs())

    with pytest.raises(Exception):  # sqlite3.IntegrityError
        dev_ops.create_item(conn, **_create_kwargs())


def test_create_item_records_audit_event(conn):
    dev_ops.create_item(conn, **_create_kwargs())

    row = conn.execute(
        "SELECT event_type, actor, source, item_id FROM audit_log WHERE event_type='item.created'"
    ).fetchone()
    assert tuple(row) == ("item.created", "ui.dev", "csn", "1")


def test_update_item_changes_fields(conn):
    _insert_item(conn, "csn", "1", extracted_title="Old", policy="informational")

    updated = dev_ops.update_item(
        conn,
        "csn",
        "1",
        extracted_title="New",
        extracted_contents="New body",
        summary="A summary",
        url="http://new.test",
        event_key="ek",
        type_="Evento",
        subtype="Sismo",
        policy="urgent",
        source_date_time="2026-01-01T00:00:00+00:00",
    )

    assert updated is True
    row = conn.execute(
        "SELECT extracted_title, extracted_contents, summary, url, event_key, type, subtype, "
        "policy, source_date_time FROM items WHERE source='csn' AND item_id='1'"
    ).fetchone()
    assert tuple(row) == (
        "New",
        "New body",
        "A summary",
        "http://new.test",
        "ek",
        "Evento",
        "Sismo",
        "urgent",
        "2026-01-01T00:00:00+00:00",
    )


def test_update_item_does_not_change_primary_key_columns(conn):
    _insert_item(conn, "csn", "1")

    dev_ops.update_item(
        conn,
        "csn",
        "1",
        extracted_title="New",
        extracted_contents=None,
        summary=None,
        url=None,
        event_key=None,
        type_=None,
        subtype=None,
        policy=None,
        source_date_time=None,
    )

    row = conn.execute("SELECT source, item_id FROM items WHERE item_id='1'").fetchone()
    assert tuple(row) == ("csn", "1")


def test_update_item_returns_false_for_unknown_item(conn):
    updated = dev_ops.update_item(
        conn,
        "csn",
        "does-not-exist",
        extracted_title=None,
        extracted_contents=None,
        summary=None,
        url=None,
        event_key=None,
        type_=None,
        subtype=None,
        policy=None,
        source_date_time=None,
    )
    assert updated is False


def test_update_item_records_audit_event(conn):
    _insert_item(conn, "csn", "1")

    dev_ops.update_item(
        conn,
        "csn",
        "1",
        extracted_title="New",
        extracted_contents=None,
        summary=None,
        url=None,
        event_key=None,
        type_=None,
        subtype=None,
        policy="urgent",
        source_date_time=None,
    )

    row = conn.execute(
        "SELECT event_type, source, item_id FROM audit_log WHERE event_type='item.dev_edited'"
    ).fetchone()
    assert tuple(row) == ("item.dev_edited", "csn", "1")


def test_delete_item_removes_item_row(conn):
    _insert_item(conn, "csn", "1")

    deleted = dev_ops.delete_item(conn, "csn", "1")

    assert deleted is True
    row = conn.execute("SELECT 1 FROM items WHERE source='csn' AND item_id='1'").fetchone()
    assert row is None


def test_delete_item_cascades_to_chunks_and_dispatch_state(conn):
    _insert_item(conn, "csn", "1")
    conn.execute(
        "INSERT INTO chunks (source, item_id, chunk_index, chunk_count, text) "
        "VALUES ('csn', '1', 0, 1, 'chunk text')"
    )
    conn.execute(
        "INSERT INTO trigger_dispatches (consumer, source, item_id) "
        "VALUES ('log', 'csn', '1')"
    )
    conn.execute(
        "INSERT INTO item_policy_state (consumer, source, item_id, policy) "
        "VALUES ('log', 'csn', '1', 'informational')"
    )
    conn.execute(
        "INSERT INTO beacon_tx_schedule (source, item_id, kind, ref) "
        "VALUES ('csn', '1', 'frame', '0')"
    )
    conn.commit()

    dev_ops.delete_item(conn, "csn", "1")

    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE item_id='1'").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM trigger_dispatches WHERE item_id='1'").fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM item_policy_state WHERE item_id='1'").fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM beacon_tx_schedule WHERE item_id='1'").fetchone()[0]
        == 0
    )


def test_delete_item_keeps_audit_log_history(conn):
    _insert_item(conn, "csn", "1")
    conn.execute(
        "INSERT INTO audit_log (event_type, actor, source, item_id) "
        "VALUES ('item.stored', 'adapters.storage', 'csn', '1')"
    )
    conn.commit()

    dev_ops.delete_item(conn, "csn", "1")

    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type='item.stored' AND item_id='1'"
    ).fetchone()
    assert row is not None


def test_delete_item_records_its_own_audit_event(conn):
    _insert_item(conn, "csn", "1")

    dev_ops.delete_item(conn, "csn", "1")

    row = conn.execute(
        "SELECT event_type, actor, source, item_id FROM audit_log WHERE event_type='item.deleted'"
    ).fetchone()
    assert tuple(row) == ("item.deleted", "ui.dev", "csn", "1")


def test_delete_item_returns_false_for_unknown_item(conn):
    deleted = dev_ops.delete_item(conn, "csn", "does-not-exist")
    assert deleted is False
