import threading
import time

import pytest
from adapters.storage import get_beacon_status, get_connection, set_setting

from beacon import tx_monitor
from beacon.tx_monitor import _LogTail


def _db(tmp_path):
    return get_connection(tmp_path / "radiobeacon.db")


# --------------------------------------------------------------------------
# _LogTail
# --------------------------------------------------------------------------
def test_logtail_missing_file_is_not_an_error(tmp_path):
    tail = _LogTail(str(tmp_path / "nope.log"))
    assert tail.ensure_open() is False
    assert list(tail.readlines()) == []


def test_logtail_empty_path_is_not_an_error():
    assert _LogTail("").ensure_open() is False


def test_logtail_tails_from_end_then_follows_appends(tmp_path):
    log = tmp_path / "svxlink.log"
    log.write_text("old line before we started\n")
    tail = _LogTail(str(log))

    assert tail.ensure_open() is True
    assert list(tail.readlines()) == []  # history is not replayed

    with log.open("a") as fh:
        fh.write("Tx1: Turning the transmitter ON\n")
    assert tail.ensure_open() is True
    assert list(tail.readlines()) == ["Tx1: Turning the transmitter ON\n"]


def test_logtail_follows_across_rotation(tmp_path):
    log = tmp_path / "svxlink.log"
    log.write_text("line one\n")
    tail = _LogTail(str(log))
    tail.ensure_open()

    log.unlink()
    log.write_text("Tx1: Turning the transmitter ON\n")  # fresh inode

    assert tail.ensure_open() is True
    # a rotated file is read from the start, so the first line survives
    assert "Turning the transmitter ON\n" in "".join(tail.readlines())


# --------------------------------------------------------------------------
# run_tx_monitor thread
# --------------------------------------------------------------------------
@pytest.fixture
def fast_monitor(monkeypatch, tmp_path):
    # run_tx_monitor opens its own connection against tx_monitor.DEFAULT_DB_PATH;
    # point it at this test's db so set_setting/get_beacon_status here line up.
    monkeypatch.setattr(tx_monitor, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    monkeypatch.setattr(tx_monitor, "_POLL_SECONDS", 0.02)
    monkeypatch.setattr(tx_monitor, "_HEARTBEAT_SECONDS", 0.0)
    monkeypatch.setattr(tx_monitor, "_RETRY_SECONDS", 0.05)


def _wait_for(fn, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


def _run(stop_event):
    t = threading.Thread(target=tx_monitor.run_tx_monitor, args=(stop_event,), daemon=True)
    t.start()
    return t


def test_monitor_tracks_key_up_and_key_down(tmp_path, fast_monitor):
    conn = _db(tmp_path)
    log = tmp_path / "svxlink.log"
    log.write_text("")
    set_setting(conn, "BEACON_SVXLINK_LOG_PATH", str(log))

    stop = threading.Event()
    thread = _run(stop)
    try:
        assert _wait_for(lambda: get_beacon_status(conn, "tx_monitor_heartbeat_at"))

        with log.open("a") as fh:
            fh.write("Tx1: Turning the transmitter ON\n")
        assert _wait_for(lambda: get_beacon_status(conn, "tx_keyed") == "1")
        assert get_beacon_status(conn, "tx_keyed_at")

        with log.open("a") as fh:
            fh.write("Tx1: Turning the transmitter OFF\n")
        assert _wait_for(lambda: get_beacon_status(conn, "tx_keyed") == "0")
        assert get_beacon_status(conn, "tx_unkeyed_at")
    finally:
        stop.set()
        thread.join(timeout=2)


def test_monitor_dormant_when_log_absent(tmp_path, fast_monitor):
    conn = _db(tmp_path)
    set_setting(conn, "BEACON_SVXLINK_LOG_PATH", str(tmp_path / "does-not-exist.log"))

    stop = threading.Event()
    thread = _run(stop)
    try:
        time.sleep(0.3)
        assert get_beacon_status(conn, "tx_monitor_heartbeat_at") is None
        assert get_beacon_status(conn, "tx_keyed") is None
    finally:
        stop.set()
        thread.join(timeout=2)


def test_monitor_disabled_writes_nothing(tmp_path, fast_monitor):
    conn = _db(tmp_path)
    log = tmp_path / "svxlink.log"
    log.write_text("Tx1: Turning the transmitter ON\n")
    set_setting(conn, "BEACON_SVXLINK_LOG_PATH", str(log))
    set_setting(conn, "BEACON_TX_MONITOR_ENABLED", "false")

    stop = threading.Event()
    thread = _run(stop)
    try:
        time.sleep(0.3)
        assert get_beacon_status(conn, "tx_monitor_heartbeat_at") is None
    finally:
        stop.set()
        thread.join(timeout=2)


def test_monitor_watchdog_clears_stuck_key(tmp_path, fast_monitor, monkeypatch):
    monkeypatch.setattr(tx_monitor, "_MAX_KEYED_SECONDS", 0.6)
    conn = _db(tmp_path)
    log = tmp_path / "svxlink.log"
    log.write_text("")
    set_setting(conn, "BEACON_SVXLINK_LOG_PATH", str(log))

    stop = threading.Event()
    thread = _run(stop)
    try:
        assert _wait_for(lambda: get_beacon_status(conn, "tx_monitor_heartbeat_at"))
        with log.open("a") as fh:
            fh.write("Tx1: Turning the transmitter ON\n")  # never an OFF
        assert _wait_for(lambda: get_beacon_status(conn, "tx_keyed") == "1")
        # watchdog forces it back to idle without an OFF line
        assert _wait_for(lambda: get_beacon_status(conn, "tx_keyed") == "0")
    finally:
        stop.set()
        thread.join(timeout=2)
