import logging

from adapters import logsetup
from adapters.storage import get_connection, set_setting


def _db(tmp_path):
    return tmp_path / "radiobeacon.db"


def test_resolve_level_defaults_to_info(tmp_path):
    assert logsetup.resolve_level(db_path=_db(tmp_path)) == logging.INFO


def test_resolve_level_reads_each_choice(tmp_path, monkeypatch):
    db = _db(tmp_path)
    for name in logsetup.LOG_LEVEL_CHOICES:
        conn = get_connection(db)
        set_setting(conn, "LOG_LEVEL", name)
        conn.close()
        assert logsetup.resolve_level(db_path=db) == logsetup._NAME_TO_LEVEL[name]


def test_resolve_level_db_row_wins_over_env(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert logsetup.resolve_level(db_path=db) == logging.ERROR  # env, no row yet
    conn = get_connection(db)
    set_setting(conn, "LOG_LEVEL", "DEBUG")
    conn.close()
    assert logsetup.resolve_level(db_path=db) == logging.DEBUG  # row overrides env


def test_resolve_level_unknown_falls_back_to_info(tmp_path):
    db = _db(tmp_path)
    conn = get_connection(db)
    set_setting(conn, "LOG_LEVEL", "LOUD")
    conn.close()
    assert logsetup.resolve_level(db_path=db) == logging.INFO


def test_configure_logging_installs_one_handler_and_is_idempotent(tmp_path):
    root = logging.getLogger()
    before = set(root.handlers)
    try:
        logsetup.configure_logging("test", db_path=_db(tmp_path))
        logsetup.configure_logging("test", db_path=_db(tmp_path))
        added = set(root.handlers) - before
        assert len(added) == 1  # exactly one handler of ours, not two
        assert logsetup._our_handler in root.handlers
        assert root.level == logging.INFO
    finally:
        if logsetup._our_handler in root.handlers:
            root.removeHandler(logsetup._our_handler)
        logsetup._our_handler = None


def test_refresh_level_applies_a_changed_setting(tmp_path, caplog):
    db = _db(tmp_path)
    root = logging.getLogger()
    try:
        logsetup.configure_logging("test", db_path=db)
        assert root.level == logging.INFO

        conn = get_connection(db)
        set_setting(conn, "LOG_LEVEL", "DEBUG")
        conn.close()

        # capture just our logger, so root stays INFO and refresh_level
        # actually sees a change
        with caplog.at_level(logging.DEBUG, logger="adapters.logsetup"):
            logsetup.refresh_level(db_path=db)
        assert root.level == logging.DEBUG
        assert "log level" in caplog.text

        # no-op when unchanged
        caplog.clear()
        logsetup.refresh_level(db_path=db)
        assert root.level == logging.DEBUG
        assert "log level" not in caplog.text
    finally:
        if logsetup._our_handler in root.handlers:
            root.removeHandler(logsetup._our_handler)
        logsetup._our_handler = None
        root.setLevel(logging.WARNING)
