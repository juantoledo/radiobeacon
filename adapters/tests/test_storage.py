import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

import adapters.storage as storage_module
from adapters.storage import get_connection, record_audit_event, store_reading


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
