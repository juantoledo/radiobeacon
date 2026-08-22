import sqlite3

import pytest
from adapters.storage import get_connection

from ui import config


def _insert_item(conn, source, item_id, *, dispatch_policy="informational"):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, fetched_at, dispatch_policy, rawdata) "
        "VALUES (?, ?, 'Title', datetime('now'), ?, '{}')",
        (source, item_id, dispatch_policy),
    )
    conn.commit()


def test_dev_hub_returns_200(client):
    response = client.get("/dev")
    assert response.status_code == 200
    assert "Developers" in response.text


def test_nav_shows_developers_link_when_enabled(client, monkeypatch):
    monkeypatch.setattr(config, "UI_DEV_TOOLS_ENABLED", True)

    response = client.get("/")

    assert 'href="/dev"' in response.text


def test_dev_routes_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "UI_DEV_TOOLS_ENABLED", False)

    assert client.get("/dev").status_code == 404
    assert client.get("/dev/items/new").status_code == 404
    assert client.get("/dev/sql").status_code == 404


def test_nav_hides_developers_link_when_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "UI_DEV_TOOLS_ENABLED", False)

    response = client.get("/")

    assert 'href="/dev"' not in response.text


def test_new_item_page_returns_200(client):
    response = client.get("/dev/items/new")
    assert response.status_code == 200
    assert "New item" in response.text


def test_new_item_page_suggests_a_uuid_for_item_id(client):
    import re
    import uuid

    response = client.get("/dev/items/new")

    match = re.search(
        r'id="item_id"[^>]*value="([0-9a-f-]+)"', response.text
    )
    assert match is not None
    # Raises ValueError if it isn't a well-formed UUID.
    uuid.UUID(match.group(1))


def test_new_item_page_suggests_a_different_uuid_each_request(client):
    import re

    first = re.search(
        r'id="item_id"[^>]*value="([0-9a-f-]+)"', client.get("/dev/items/new").text
    ).group(1)
    second = re.search(
        r'id="item_id"[^>]*value="([0-9a-f-]+)"', client.get("/dev/items/new").text
    ).group(1)

    assert first != second


def test_new_item_page_suggests_informational_dispatch_policy(client):
    import re

    response = client.get("/dev/items/new")

    match = re.search(r'id="dispatch_policy"[^>]*value="([^"]*)"', response.text)
    assert match is not None
    assert match.group(1) == "informational"


def test_new_item_page_suggests_current_source_date_time(client):
    import re
    from datetime import datetime, timedelta, timezone

    response = client.get("/dev/items/new")

    match = re.search(r'id="source_date_time"[^>]*value="([^"]*)"', response.text)
    assert match is not None
    suggested = datetime.fromisoformat(match.group(1))
    assert suggested.tzinfo is not None
    assert abs(datetime.now(timezone.utc) - suggested) < timedelta(seconds=10)


def test_edit_item_page_does_not_override_existing_dispatch_policy_or_date(client, conn):
    conn.execute(
        "INSERT INTO items (source, item_id, fetched_at, dispatch_policy, source_date_time, rawdata) "
        "VALUES ('csn', '1', datetime('now'), 'urgent', '2020-01-01T00:00:00+00:00', '{}')"
    )
    conn.commit()

    response = client.get("/dev/items/csn/1/edit")

    assert 'id="dispatch_policy"' in response.text
    assert 'value="urgent"' in response.text
    assert 'value="2020-01-01T00:00:00+00:00"' in response.text


def test_create_item_redirects_and_persists(client, conn):
    response = client.post(
        "/dev/items",
        data={
            "source": "csn",
            "item_id": "new-1",
            "extracted_title": "Manually created",
            "dispatch_policy": "urgent",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/items/csn/new-1?msg=item+created"
    row = conn.execute(
        "SELECT extracted_title, dispatch_policy FROM items WHERE source='csn' AND item_id='new-1'"
    ).fetchone()
    assert tuple(row) == ("Manually created", "urgent")


def test_create_item_duplicate_pk_redirects_back_to_form(client, conn):
    _insert_item(conn, "csn", "dup")

    response = client.post(
        "/dev/items",
        data={"source": "csn", "item_id": "dup", "extracted_title": "x"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/dev/items/new")


def test_edit_item_page_returns_200_for_known_item(client, conn):
    _insert_item(conn, "csn", "1")

    response = client.get("/dev/items/csn/1/edit")

    assert response.status_code == 200
    assert "Edit item" in response.text


def test_edit_item_page_renders_null_fields_as_empty_not_the_word_none(client, conn):
    # _insert_item only sets extracted_title/dispatch_policy — every other
    # editable column (extracted_contents, summary, url, event_key, type,
    # subtype, source_date_time) is genuinely NULL. Regression test for a
    # bug where those fields rendered the literal text "None" instead of
    # an empty input.
    _insert_item(conn, "csn", "1")

    response = client.get("/dev/items/csn/1/edit")

    assert response.status_code == 200
    assert ">None<" not in response.text
    assert "None</textarea>" not in response.text
    assert 'value="None"' not in response.text


def test_edit_item_page_returns_404_for_unknown_item(client):
    response = client.get("/dev/items/csn/does-not-exist/edit")
    assert response.status_code == 404


def test_update_item_action_redirects_to_detail_page(client, conn):
    _insert_item(conn, "csn", "1", dispatch_policy="informational")

    response = client.post(
        "/dev/items/csn/1",
        data={"extracted_title": "Updated", "dispatch_policy": "urgent"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/items/csn/1")
    row = conn.execute(
        "SELECT extracted_title, dispatch_policy FROM items WHERE item_id='1'"
    ).fetchone()
    assert tuple(row) == ("Updated", "urgent")


def test_delete_item_action_redirects_to_hub_and_removes_row(client, conn):
    _insert_item(conn, "csn", "1")

    response = client.post("/dev/items/csn/1/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/dev")
    row = conn.execute("SELECT 1 FROM items WHERE item_id='1'").fetchone()
    assert row is None


def test_reset_dispatch_state_action_redirects_to_edit_page(client, conn):
    _insert_item(conn, "csn", "1")

    response = client.post(
        "/dev/items/csn/1/reset-dispatch", data={"consumer": "log"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/dev/items/csn/1/edit")


def test_sql_runner_returns_200_with_no_query(client):
    response = client.get("/dev/sql")
    assert response.status_code == 200
    assert "SQL runner" in response.text


def test_sql_runner_executes_select_and_shows_results(client, monkeypatch, tmp_path):
    # /dev/sql reads via ui.db.open_readonly_connection(), which is NOT
    # wired through the get_db dependency override the `client`/`conn`
    # fixtures use elsewhere (see that function's docstring) — it opens its
    # own connection straight at config.UI_DB_PATH (or DEFAULT_DB_PATH), so
    # this test needs a real file at that path, not the shared in-memory
    # `conn` fixture.
    db_path = tmp_path / "radiobeacon.db"
    monkeypatch.setattr(config, "UI_DB_PATH", str(db_path))
    file_conn = get_connection(db_path)
    _insert_item(file_conn, "csn", "1")
    file_conn.close()

    response = client.get("/dev/sql", params={"q": "SELECT source, item_id FROM items"})

    assert response.status_code == 200
    assert "csn" in response.text
    assert ">1<" in response.text or "csn" in response.text  # row rendered


def test_sql_runner_rejects_write_statement(client):
    response = client.get("/dev/sql", params={"q": "DELETE FROM items"})

    assert response.status_code == 200
    assert "only SELECT" in response.text


def test_sql_runner_rejects_stacked_statements(client):
    response = client.get(
        "/dev/sql", params={"q": "SELECT * FROM items; DROP TABLE items"}
    )

    assert response.status_code == 200
    assert "single statement" in response.text


def test_open_readonly_connection_blocks_writes_at_the_driver_level(tmp_path, monkeypatch):
    # Belt-and-braces: exercise the SQL runner's *actual* safety boundary
    # (a mode=ro SQLite connection), not just ui.sql_guard's keyword
    # pre-check — a write reaching SQLite through this connection must
    # fail at the driver level regardless of what validation did or
    # didn't catch. Uses a real on-disk file (mode=ro is a filesystem-
    # level open mode, meaningless for ":memory:") isolated to tmp_path,
    # never the real storage/radiobeacon.db.
    from ui.db import open_readonly_connection

    db_path = tmp_path / "readonly-test.db"
    setup_conn = get_connection(db_path)
    setup_conn.execute(
        "INSERT INTO items (source, item_id, fetched_at, rawdata) "
        "VALUES ('csn', '1', datetime('now'), '{}')"
    )
    setup_conn.commit()
    setup_conn.close()

    monkeypatch.setattr(config, "UI_DB_PATH", str(db_path))

    ro_conn = open_readonly_connection()
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute("UPDATE items SET dispatch_policy = 'urgent'")
    finally:
        ro_conn.close()
