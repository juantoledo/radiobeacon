import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

import pytest

import adapters.storage as storage_module
from adapters.storage import (
    delete_setting,
    get_beacon_status,
    get_connection,
    get_setting,
    list_beacon_status,
    list_settings,
    record_audit_event,
    set_beacon_status,
    set_setting,
    store_chunks,
    store_reading,
    store_summary,
)


@dataclass
class FakeAlert:
    id: str
    titulo: str
    title: str = "Alerta de prueba"
    contents: str = "Contenido de prueba"
    url: str = "https://example.com/alerta/1"
    event_key: str = "alerta-de-prueba-2026-08-19"
    type: str = "Alerta"
    subtype: str = "Hidrometeorologico"
    dispatch_policy: str = "urgent"
    source_date_time: datetime = datetime(2026, 8, 19, 10, 0, 0)


@dataclass
class FakeAlertWithoutId:
    titulo: str


@dataclass
class FakeAlertMinimal:
    """No title/contents/url/event_key/source_date_time — only the
    required `id`."""

    id: str


def _make_reading(**overrides):
    defaults = dict(
        source="fake_source",
        fetched_at=datetime(2026, 8, 19, 12, 0, 0),
        ok=True,
        error=None,
        data=[FakeAlert(id="1", titulo="Alerta de prueba")],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_store_reading_records_audit_event_for_newly_stored_item(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    store_reading(conn, _make_reading())

    row = conn.execute(
        "SELECT event_type, actor, source, item_id FROM audit_log WHERE event_type = 'item.stored'"
    ).fetchone()
    assert row == ("item.stored", "adapters.storage", "fake_source", "1")


def test_store_reading_does_not_record_audit_event_for_already_known_item(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    store_reading(conn, _make_reading())

    store_reading(conn, _make_reading())  # same id, refetched

    count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE event_type = 'item.stored'"
    ).fetchone()[0]
    assert count == 1  # only the first, genuinely-new store recorded


def test_store_reading_populates_items_table(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    stored = store_reading(conn, _make_reading())

    assert stored == 1
    row = conn.execute(
        "SELECT source, item_id, extracted_title, extracted_contents, "
        "summary, url, event_key, type, subtype, dispatch_policy, "
        "source_date_time, rawdata "
        "FROM items WHERE item_id = ?",
        ("1",),
    ).fetchone()
    assert row[0] == "fake_source"
    assert row[1] == "1"
    assert row[2] == "Alerta de prueba"
    assert row[3] == "Contenido de prueba"
    # store_reading() never touches summary — a separate actor sets it later,
    # not the fetch/store path
    assert row[4] is None
    assert row[5] == "https://example.com/alerta/1"
    assert row[6] == "alerta-de-prueba-2026-08-19"
    assert row[7] == "Alerta"
    assert row[8] == "Hidrometeorologico"
    assert row[9] == "urgent"
    assert row[10] == "2026-08-19T10:00:00"
    assert json.loads(row[11])["id"] == "1"


def test_store_reading_handles_missing_generic_fields(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    reading = _make_reading(data=[FakeAlertMinimal(id="2")])

    stored = store_reading(conn, reading)

    assert stored == 1
    row = conn.execute(
        "SELECT extracted_title, extracted_contents, summary, "
        "url, event_key, type, subtype, dispatch_policy, source_date_time "
        "FROM items WHERE item_id = ?",
        ("2",),
    ).fetchone()
    assert row == (None, None, None, None, None, None, None, None, None)


def test_store_reading_preserves_existing_summary_on_refresh(tmp_path):
    """A `summary` set later by a separate actor (manually or via an
    orchestrator) must survive a normal refresh fetch — store_reading()
    never touches an already-known row at all (see
    test_store_reading_does_not_touch_known_items)."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    store_reading(conn, _make_reading())
    conn.execute(
        "UPDATE items SET summary = ? WHERE item_id = ?", ("hand-written summary", "1")
    )

    store_reading(conn, _make_reading())  # same id, refetched

    row = conn.execute(
        "SELECT summary FROM items WHERE item_id = ?", ("1",)
    ).fetchone()
    assert row == ("hand-written summary",)


def test_store_reading_groups_related_updates_by_event_key(tmp_path):
    """The actual use case: separate items sharing the same event_key are
    an event's history (declared -> monitored -> ...), reconstructable by
    querying our own stored rows, no extra API calls needed."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    shared_key = "se-declara-alerta-amarilla-2026-08-16"
    store_reading(
        conn,
        _make_reading(
            data=[
                FakeAlert(
                    id="1",
                    titulo="Se declara Alerta Amarilla",
                    title="Se declara Alerta Amarilla",
                    event_key=shared_key,
                    source_date_time=datetime(2026, 8, 16, 21, 7, 0),
                )
            ]
        ),
    )
    store_reading(
        conn,
        _make_reading(
            data=[
                FakeAlert(
                    id="2",
                    titulo="Monitoreo Alerta Amarilla",
                    title="Monitoreo Alerta Amarilla",
                    event_key=shared_key,
                    source_date_time=datetime(2026, 8, 17, 5, 37, 0),
                )
            ]
        ),
    )
    store_reading(
        conn,
        _make_reading(
            data=[
                FakeAlert(
                    id="3",
                    titulo="Otra alerta no relacionada",
                    title="Otra alerta no relacionada",
                    event_key="otro-evento-2026-08-18",
                    source_date_time=datetime(2026, 8, 18, 0, 0, 0),
                )
            ]
        ),
    )

    history = conn.execute(
        "SELECT item_id, extracted_title FROM items "
        "WHERE event_key = ? ORDER BY source_date_time",
        (shared_key,),
    ).fetchall()

    assert history == [
        ("1", "Se declara Alerta Amarilla"),
        ("2", "Monitoreo Alerta Amarilla"),
    ]


def test_store_reading_does_not_duplicate_items_with_same_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    first = store_reading(conn, _make_reading())
    second = store_reading(conn, _make_reading())  # same item id, fetched again

    assert first == 1
    assert second == 0
    count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE item_id = ?", ("1",)
    ).fetchone()[0]
    assert count == 1


def test_store_reading_does_not_touch_known_items(tmp_path):
    """Items are immutable once stored (true for SENAPRED: a new update is
    always a new id, never a mutation of an existing one) — a known id is
    left exactly as first seen, never refreshed, even if a later fetch
    would supply richer/different field values under the same id."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    store_reading(conn, _make_reading(data=[FakeAlertMinimal(id="1")]))

    stored = store_reading(conn, _make_reading())  # same id, now with generic fields

    assert stored == 0  # not newly seen, and NOT refreshed in place
    row = conn.execute(
        "SELECT extracted_title, extracted_contents FROM items WHERE item_id = ?",
        ("1",),
    ).fetchone()
    assert row == (None, None)  # still the minimal version from the first store


def test_store_reading_skips_items_without_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    reading = _make_reading(data=[FakeAlertWithoutId(titulo="sin id")])

    stored = store_reading(conn, reading)

    assert stored == 0
    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert count == 0


def test_store_reading_handles_empty_data(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    reading = _make_reading(ok=False, error="network unreachable", data=[])

    stored = store_reading(conn, reading)

    assert stored == 0


def test_get_connection_is_idempotent(tmp_path):
    db_path = tmp_path / "radiobeacon.db"

    conn1 = get_connection(db_path)
    store_reading(conn1, _make_reading())
    conn1.close()

    conn2 = get_connection(db_path)
    count = conn2.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    assert count == 1
    assert isinstance(conn2, sqlite3.Connection)


def test_get_connection_enables_wal_mode(tmp_path):
    db_path = tmp_path / "radiobeacon.db"

    conn = get_connection(db_path)

    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_get_connection_migrates_legacy_data_column(tmp_path):
    db_path = tmp_path / "radiobeacon.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        "CREATE TABLE items (source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "fetched_at TEXT NOT NULL, captured_at TEXT NOT NULL DEFAULT "
        "(datetime('now')), data TEXT NOT NULL, PRIMARY KEY (source, item_id))"
    )
    legacy_conn.execute(
        "INSERT INTO items (source, item_id, fetched_at, data) VALUES (?, ?, ?, ?)",
        ("fake_source", "legacy-1", "2026-08-19T12:00:00", '{"id": "legacy-1"}'),
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = get_connection(db_path)

    row = conn.execute(
        "SELECT rawdata, extracted_title, extracted_contents, "
        "summary, url, event_key, type, subtype, dispatch_policy, "
        "source_date_time "
        "FROM items WHERE item_id = ?",
        ("legacy-1",),
    ).fetchone()
    assert json.loads(row[0]) == {"id": "legacy-1"}
    assert row[1:] == (None, None, None, None, None, None, None, None, None)


def test_get_connection_migrates_title_and_contents_columns(tmp_path):
    """Covers the intermediate schema (title/contents, before they were
    renamed to extracted_title/extracted_contents) that a real database
    may already be in."""
    db_path = tmp_path / "radiobeacon.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        "CREATE TABLE items (source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "title TEXT, contents TEXT, url TEXT, source_date_time TEXT, "
        "fetched_at TEXT NOT NULL, captured_at TEXT NOT NULL DEFAULT "
        "(datetime('now')), rawdata TEXT NOT NULL, PRIMARY KEY (source, item_id))"
    )
    legacy_conn.execute(
        "INSERT INTO items (source, item_id, title, contents, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "fake_source",
            "legacy-2",
            "Titulo viejo",
            "Contenido viejo",
            "2026-08-19T12:00:00",
            '{"id": "legacy-2"}',
        ),
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = get_connection(db_path)

    row = conn.execute(
        "SELECT extracted_title, extracted_contents FROM items WHERE item_id = ?",
        ("legacy-2",),
    ).fetchone()
    assert row == ("Titulo viejo", "Contenido viejo")


def test_get_connection_migrates_missing_event_key_column(tmp_path):
    """Covers a database created before event_key/url_access existed at
    all — the column is added fresh, backfilled NULL."""
    db_path = tmp_path / "radiobeacon.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        "CREATE TABLE items (source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "extracted_title TEXT, extracted_contents TEXT, summary TEXT, "
        "url TEXT, source_date_time TEXT, fetched_at TEXT NOT NULL, "
        "captured_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "rawdata TEXT NOT NULL, PRIMARY KEY (source, item_id))"
    )
    legacy_conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?)",
        ("fake_source", "legacy-4", "Titulo", "2026-08-19T12:00:00", '{"id": "legacy-4"}'),
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = get_connection(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    assert "event_key" in columns

    row = conn.execute(
        "SELECT event_key FROM items WHERE item_id = ?", ("legacy-4",)
    ).fetchone()
    assert row == (None,)  # backfilled NULL; populated on next real fetch


def test_get_connection_migrates_missing_type_and_subtype_columns(tmp_path):
    """Covers a database created before type/subtype existed at all — the
    columns are added fresh, backfilled NULL."""
    db_path = tmp_path / "radiobeacon.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        "CREATE TABLE items (source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "extracted_title TEXT, extracted_contents TEXT, summary TEXT, "
        "url TEXT, event_key TEXT, source_date_time TEXT, "
        "fetched_at TEXT NOT NULL, captured_at TEXT NOT NULL DEFAULT "
        "(datetime('now')), rawdata TEXT NOT NULL, PRIMARY KEY (source, item_id))"
    )
    legacy_conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?)",
        ("fake_source", "legacy-6", "Titulo", "2026-08-19T12:00:00", '{"id": "legacy-6"}'),
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = get_connection(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    assert "type" in columns
    assert "subtype" in columns

    row = conn.execute(
        "SELECT type, subtype FROM items WHERE item_id = ?", ("legacy-6",)
    ).fetchone()
    assert row == (None, None)


def test_get_connection_migrates_missing_dispatch_policy_column(tmp_path):
    """Covers a database created before dispatch_policy existed at all —
    the column is added fresh, backfilled NULL."""
    db_path = tmp_path / "radiobeacon.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        "CREATE TABLE items (source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "extracted_title TEXT, extracted_contents TEXT, summary TEXT, "
        "url TEXT, event_key TEXT, type TEXT, subtype TEXT, source_date_time TEXT, "
        "fetched_at TEXT NOT NULL, captured_at TEXT NOT NULL DEFAULT "
        "(datetime('now')), rawdata TEXT NOT NULL, PRIMARY KEY (source, item_id))"
    )
    legacy_conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?)",
        ("fake_source", "legacy-7", "Titulo", "2026-08-19T12:00:00", '{"id": "legacy-7"}'),
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = get_connection(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    assert "dispatch_policy" in columns

    row = conn.execute(
        "SELECT dispatch_policy FROM items WHERE item_id = ?", ("legacy-7",)
    ).fetchone()
    assert row == (None,)


def test_get_connection_drops_urgency_and_repeat_columns(tmp_path):
    """Covers the schema with the now-retired urgency/repeat_times/
    repeat_interval_seconds columns (superseded by dispatch_policy,
    which centralizes that config in dispatcher's dispatch_policies table
    instead) — dropped in place, same mechanism that already retired
    summarized_title/summarized_contents."""
    db_path = tmp_path / "radiobeacon.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        "CREATE TABLE items (source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "extracted_title TEXT, extracted_contents TEXT, summary TEXT, "
        "url TEXT, event_key TEXT, type TEXT, subtype TEXT, urgency TEXT, "
        "repeat_times INTEGER, repeat_interval_seconds INTEGER, "
        "source_date_time TEXT, fetched_at TEXT NOT NULL, captured_at TEXT NOT NULL "
        "DEFAULT (datetime('now')), rawdata TEXT NOT NULL, PRIMARY KEY (source, item_id))"
    )
    legacy_conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, urgency, repeat_times, "
        "repeat_interval_seconds, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "fake_source",
            "legacy-8",
            "Titulo",
            "urgent",
            5,
            60,
            "2026-08-19T12:00:00",
            '{"id": "legacy-8"}',
        ),
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = get_connection(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    assert "urgency" not in columns
    assert "repeat_times" not in columns
    assert "repeat_interval_seconds" not in columns
    assert "dispatch_policy" in columns

    row = conn.execute(
        "SELECT extracted_title, dispatch_policy FROM items WHERE item_id = ?",
        ("legacy-8",),
    ).fetchone()
    # other data preserved; dropped columns' data is gone, dispatch_policy
    # starts NULL until re-set (by an adapter re-fetching under a new id,
    # or manually via dispatcher/override_item.py)
    assert row == ("Titulo", None)


def test_get_connection_renames_url_access_to_event_key(tmp_path):
    """Covers the schema with the column's previous name, url_access —
    renamed in place to event_key, data preserved."""
    db_path = tmp_path / "radiobeacon.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        "CREATE TABLE items (source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "extracted_title TEXT, extracted_contents TEXT, summary TEXT, "
        "url TEXT, url_access TEXT, source_date_time TEXT, "
        "fetched_at TEXT NOT NULL, captured_at TEXT NOT NULL DEFAULT "
        "(datetime('now')), rawdata TEXT NOT NULL, PRIMARY KEY (source, item_id))"
    )
    legacy_conn.execute(
        "INSERT INTO items (source, item_id, url_access, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "fake_source",
            "legacy-5",
            "evento-antiguo-2026-08-01",
            "2026-08-19T12:00:00",
            '{"id": "legacy-5"}',
        ),
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = get_connection(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    assert "url_access" not in columns
    assert "event_key" in columns

    row = conn.execute(
        "SELECT event_key FROM items WHERE item_id = ?", ("legacy-5",)
    ).fetchone()
    assert row == ("evento-antiguo-2026-08-01",)


def test_get_connection_migrates_summarized_title_contents_to_summary(tmp_path):
    """Covers the schema with separate summarized_title/summarized_contents
    columns (before they were merged into one `summary` column)."""
    db_path = tmp_path / "radiobeacon.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        "CREATE TABLE items (source TEXT NOT NULL, item_id TEXT NOT NULL, "
        "extracted_title TEXT, extracted_contents TEXT, "
        "summarized_title TEXT, summarized_contents TEXT, "
        "url TEXT, source_date_time TEXT, fetched_at TEXT NOT NULL, "
        "captured_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "rawdata TEXT NOT NULL, PRIMARY KEY (source, item_id))"
    )
    legacy_conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, extracted_contents, "
        "summarized_title, summarized_contents, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "fake_source",
            "legacy-3",
            "Titulo",
            "Contenido",
            "Titulo resumido",
            "Contenido resumido",
            "2026-08-19T12:00:00",
            '{"id": "legacy-3"}',
        ),
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = get_connection(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    assert "summarized_title" not in columns
    assert "summarized_contents" not in columns
    assert "summary" in columns

    row = conn.execute(
        "SELECT extracted_title, extracted_contents, summary "
        "FROM items WHERE item_id = ?",
        ("legacy-3",),
    ).fetchone()
    # dropped columns' data is gone; summary starts NULL until re-fetched
    assert row == ("Titulo", "Contenido", None)


def test_register_audit_event_hook_is_invoked_after_record_audit_event(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "_audit_event_hooks", [])
    conn = get_connection(tmp_path / "radiobeacon.db")
    calls = []
    storage_module.register_audit_event_hook(lambda **kwargs: calls.append(kwargs))

    record_audit_event(
        conn,
        event_type="item.dispatched",
        actor="log_handler",
        source="senapred",
        item_id="1",
        details={"consumer": "log"},
    )

    assert calls == [
        {
            "event_type": "item.dispatched",
            "actor": "log_handler",
            "source": "senapred",
            "item_id": "1",
            "details": {"consumer": "log"},
        }
    ]


def test_register_audit_event_hook_receives_none_for_optional_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "_audit_event_hooks", [])
    conn = get_connection(tmp_path / "radiobeacon.db")
    calls = []
    storage_module.register_audit_event_hook(lambda **kwargs: calls.append(kwargs))

    record_audit_event(conn, event_type="policy.set", actor="dispatcher.policy")

    assert calls == [
        {
            "event_type": "policy.set",
            "actor": "dispatcher.policy",
            "source": None,
            "item_id": None,
            "details": None,
        }
    ]


def test_raising_hook_does_not_propagate_or_block_other_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "_audit_event_hooks", [])
    conn = get_connection(tmp_path / "radiobeacon.db")
    calls = []

    def failing_hook(**kwargs):
        raise RuntimeError("boom")

    storage_module.register_audit_event_hook(failing_hook)
    storage_module.register_audit_event_hook(lambda **kwargs: calls.append(kwargs))

    record_audit_event(conn, event_type="item.discovered", actor="dispatcher.watcher")

    assert len(calls) == 1
    row = conn.execute(
        "SELECT event_type FROM audit_log WHERE event_type = 'item.discovered'"
    ).fetchone()
    assert row is not None


def test_registering_the_same_hook_twice_only_invokes_it_once(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "_audit_event_hooks", [])
    conn = get_connection(tmp_path / "radiobeacon.db")
    calls = []

    def hook(**kwargs):
        calls.append(kwargs)

    storage_module.register_audit_event_hook(hook)
    storage_module.register_audit_event_hook(hook)

    record_audit_event(conn, event_type="item.rearmed", actor="dispatcher.override")

    assert len(calls) == 1


def test_get_connection_closes_connection_on_setup_failure(tmp_path, monkeypatch):
    created = []
    real_connect = sqlite3.connect

    def _tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    monkeypatch.setattr(storage_module.sqlite3, "connect", _tracking_connect)

    def _raise(conn):
        raise RuntimeError("boom")

    monkeypatch.setattr(storage_module, "_migrate_items_table", _raise)

    with pytest.raises(RuntimeError):
        get_connection(tmp_path / "radiobeacon.db")

    assert len(created) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        created[0].execute("SELECT 1")


def test_migrate_items_table_tolerates_concurrent_rename(tmp_path):
    """Simulates the TOCTOU race directly: another connection completing
    the exact same rename between this connection's PRAGMA table_info
    read and its own ALTER TABLE call. _migrate_items_table must treat
    the resulting sqlite3.OperationalError as a no-op, not propagate it."""
    real_conn = sqlite3.connect(tmp_path / "radiobeacon.db")
    real_conn.execute(
        "CREATE TABLE items (source TEXT, item_id TEXT, data TEXT, fetched_at TEXT)"
    )
    real_conn.commit()

    class FlakyConn:
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("ALTER TABLE items RENAME COLUMN data"):
                raise sqlite3.OperationalError("duplicate column name: rawdata")
            return real_conn.execute(sql, *args, **kwargs)

    storage_module._migrate_items_table(FlakyConn())  # must not raise


def test_store_reading_skips_failing_item_but_commits_the_rest(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    # Not a dataclass — json.dumps(item, default=_json_default) raises
    # TypeError for it, simulating an unexpected/malformed item mid-batch.
    poison = SimpleNamespace(id="poison")
    reading = _make_reading(
        data=[
            FakeAlert(id="1", titulo="one"),
            poison,
            FakeAlert(id="3", titulo="three"),
        ]
    )

    stored = store_reading(conn, reading)

    assert stored == 2
    ids = {row[0] for row in conn.execute("SELECT item_id FROM items").fetchall()}
    assert ids == {"1", "3"}


def test_store_chunks_persists_rows_in_order(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    # Deliberately passed out of order — chunk_index, not insertion order,
    # is what determines the reconstructed sequence.
    chunks = [
        {"source": "csn", "item_id": "1", "chunk_index": 2, "chunk_count": 3, "text": "third"},
        {"source": "csn", "item_id": "1", "chunk_index": 0, "chunk_count": 3, "text": "first"},
        {"source": "csn", "item_id": "1", "chunk_index": 1, "chunk_count": 3, "text": "second"},
    ]

    store_chunks(conn, chunks)

    rows = conn.execute(
        "SELECT chunk_index, text FROM chunks WHERE source = ? AND item_id = ? ORDER BY chunk_index",
        ("csn", "1"),
    ).fetchall()
    assert rows == [(0, "first"), (1, "second"), (2, "third")]


def test_store_chunks_is_idempotent_for_the_same_batch(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    chunks = [
        {"source": "csn", "item_id": "1", "chunk_index": 0, "chunk_count": 1, "text": "only"},
    ]

    store_chunks(conn, chunks)
    second = store_chunks(conn, chunks)

    assert second == 0
    count = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE source = ? AND item_id = ?", ("csn", "1")
    ).fetchone()[0]
    assert count == 1


def test_store_chunks_returns_count_of_newly_stored_rows(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    chunks = [
        {"source": "csn", "item_id": "1", "chunk_index": 0, "chunk_count": 2, "text": "a"},
        {"source": "csn", "item_id": "1", "chunk_index": 1, "chunk_count": 2, "text": "b"},
    ]

    stored = store_chunks(conn, chunks)

    assert stored == 2


def test_store_summary_updates_existing_item(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?)",
        ("csn", "1", "Sismo", "2026-08-19T12:00:00", "{}"),
    )
    conn.commit()

    updated = store_summary(conn, "csn", "1", "A short summary.")

    assert updated is True
    row = conn.execute(
        "SELECT summary FROM items WHERE source = ? AND item_id = ?", ("csn", "1")
    ).fetchone()
    assert row[0] == "A short summary."


def test_store_summary_returns_false_for_unknown_item(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    updated = store_summary(conn, "csn", "does-not-exist", "A short summary.")

    assert updated is False


# --- settings ---


def test_get_setting_returns_db_value_when_row_exists(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "999")

    assert get_setting("ACTIONS_CHUNK_MAX_CHARS", conn=conn) == "999"


def test_get_setting_falls_back_to_env_var_when_no_row(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "42")

    assert get_setting("ACTIONS_CHUNK_MAX_CHARS", conn=conn) == "42"


def test_get_setting_falls_back_to_hardcoded_default(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.delenv("ACTIONS_CHUNK_MAX_CHARS", raising=False)

    assert get_setting("ACTIONS_CHUNK_MAX_CHARS", "200", conn=conn) == "200"
    assert get_setting("ACTIONS_CHUNK_MAX_CHARS", conn=conn) is None


def test_get_setting_db_row_takes_precedence_over_env_var(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "42")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "999")

    assert get_setting("ACTIONS_CHUNK_MAX_CHARS", conn=conn) == "999"


def test_get_setting_env_fallback_false_skips_env_var(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.setenv("SOME_KEY", "from-env")

    assert get_setting("SOME_KEY", "default", conn=conn, env_fallback=False) == "default"


def test_get_setting_env_fallback_true_is_unchanged_default_behavior(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.setenv("SOME_KEY", "from-env")

    assert get_setting("SOME_KEY", "default", conn=conn) == "from-env"


def test_get_setting_env_fallback_false_still_prefers_db_row(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.setenv("SOME_KEY", "from-env")
    set_setting(conn, "SOME_KEY", "from-db")

    assert get_setting("SOME_KEY", conn=conn, env_fallback=False) == "from-db"


def test_get_setting_without_conn_opens_and_closes_its_own(tmp_path):
    db_path = tmp_path / "radiobeacon.db"
    conn = get_connection(db_path)
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "999")

    assert get_setting("ACTIONS_CHUNK_MAX_CHARS", db_path=db_path) == "999"


def test_set_setting_upserts(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "111")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "222")

    rows = list_settings(conn)
    assert len(rows) == 1
    assert rows[0][1] == "222"  # (key, value, is_secret, updated_at, updated_by)


def test_set_setting_records_audit_event_with_value_for_non_secret(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "111", actor="ui.config")

    row = conn.execute(
        "SELECT actor, details FROM audit_log WHERE event_type = 'setting.changed'"
    ).fetchone()
    assert row[0] == "ui.config"
    details = json.loads(row[1])
    assert details == {"key": "ACTIONS_CHUNK_MAX_CHARS", "is_secret": False, "value": "111"}


def test_set_setting_never_writes_secret_plaintext_to_audit_log(tmp_path):
    """The security-critical invariant behind mask-on-read: a secret's
    audit_log.details must never carry its plaintext value, since /audit
    is a normal queryable page — writing it there would defeat the whole
    point of masking it in the config UI."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ANTHROPIC_API_KEY", "sk-super-secret-value", is_secret=True)

    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'setting.changed'"
    ).fetchone()
    details = json.loads(row[0])
    assert details == {"key": "ANTHROPIC_API_KEY", "is_secret": True}
    assert "sk-super-secret-value" not in row[0]


def test_delete_setting_removes_row_and_reverts_to_env(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.setenv("ACTIONS_CHUNK_MAX_CHARS", "42")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "999")

    deleted = delete_setting(conn, "ACTIONS_CHUNK_MAX_CHARS")

    assert deleted is True
    assert get_setting("ACTIONS_CHUNK_MAX_CHARS", conn=conn) == "42"


def test_delete_setting_returns_false_for_unknown_key(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert delete_setting(conn, "NOT_A_REAL_KEY") is False


def test_delete_setting_records_audit_event(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_CHUNK_MAX_CHARS", "999")

    delete_setting(conn, "ACTIONS_CHUNK_MAX_CHARS")

    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'setting.reset'"
    ).fetchone()
    assert json.loads(row[0]) == {"key": "ACTIONS_CHUNK_MAX_CHARS"}


def test_list_settings_orders_by_key(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ZZZ_LAST", "1")
    set_setting(conn, "AAA_FIRST", "2")

    rows = list_settings(conn)

    assert [row[0] for row in rows] == ["AAA_FIRST", "ZZZ_LAST"]


# --- beacon_status ---


def test_set_beacon_status_and_get_roundtrip(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    set_beacon_status(conn, "current_slot", "voice")

    assert get_beacon_status(conn, "current_slot") == "voice"


def test_get_beacon_status_returns_none_for_unknown_key(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert get_beacon_status(conn, "does_not_exist") is None


def test_set_beacon_status_upserts(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    set_beacon_status(conn, "current_slot", "voice")
    set_beacon_status(conn, "current_slot", "frame")

    assert get_beacon_status(conn, "current_slot") == "frame"
    rows = conn.execute("SELECT COUNT(*) FROM beacon_status").fetchone()
    assert rows[0] == 1


def test_set_beacon_status_does_not_write_audit_log(tmp_path):
    """Telemetry updated every tick must not drown out real config-change
    audit events — unlike set_setting, this is plain machine telemetry."""
    conn = get_connection(tmp_path / "radiobeacon.db")

    set_beacon_status(conn, "process_heartbeat_at", "2026-08-23T00:00:00")

    row = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
    assert row[0] == 0


def test_list_beacon_status_returns_all_as_dict(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_beacon_status(conn, "current_slot", "voice")
    set_beacon_status(conn, "voice_queue_depth", "3")

    assert list_beacon_status(conn) == {"current_slot": "voice", "voice_queue_depth": "3"}


def test_list_beacon_status_empty_when_nothing_set(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert list_beacon_status(conn) == {}
