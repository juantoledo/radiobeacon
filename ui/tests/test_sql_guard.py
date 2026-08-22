import pytest

from ui.sql_guard import InvalidQuery, ensure_select_only


def test_accepts_plain_select():
    assert ensure_select_only("SELECT * FROM items") == "SELECT * FROM items"


def test_accepts_select_case_insensitive():
    assert ensure_select_only("select * from items") == "select * from items"


def test_accepts_with_cte():
    sql = "WITH recent AS (SELECT * FROM items) SELECT * FROM recent"
    assert ensure_select_only(sql) == sql


def test_strips_trailing_semicolon_and_whitespace():
    assert ensure_select_only("  SELECT 1;  ") == "SELECT 1"


def test_rejects_empty_query():
    with pytest.raises(InvalidQuery):
        ensure_select_only("   ")


def test_rejects_non_select_statement():
    with pytest.raises(InvalidQuery):
        ensure_select_only("DELETE FROM items")


def test_rejects_insert():
    with pytest.raises(InvalidQuery):
        ensure_select_only("INSERT INTO items (source) VALUES ('x')")


def test_rejects_pragma():
    with pytest.raises(InvalidQuery):
        ensure_select_only("PRAGMA table_info(items)")


def test_rejects_drop_table():
    with pytest.raises(InvalidQuery):
        ensure_select_only("DROP TABLE items")


def test_rejects_stacked_statements_via_semicolon():
    with pytest.raises(InvalidQuery):
        ensure_select_only("SELECT * FROM items; DROP TABLE items")


def test_rejects_select_containing_disallowed_keyword_as_subquery():
    with pytest.raises(InvalidQuery):
        ensure_select_only(
            "SELECT * FROM items WHERE 1=1; UPDATE items SET dispatch_policy='urgent'"
        )


def test_rejects_query_that_neither_selects_nor_withs():
    with pytest.raises(InvalidQuery):
        ensure_select_only("EXPLAIN QUERY PLAN SELECT * FROM items")
