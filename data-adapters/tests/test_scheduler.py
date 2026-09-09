import threading

import adapters.__main__ as main_module
from adapters.__main__ import _run_adapter_loop
from adapters.policy import set_policy
from adapters.storage import get_connection, set_adapter_instance


def _interval_db(tmp_path, seconds=10):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_policy(conn, "p", fetch_kind="interval", fetch_interval_seconds=seconds)
    set_adapter_instance(conn, "fake", "api", {"url": "https://example.test"}, policy="p")
    conn.close()
    return tmp_path / "radiobeacon.db"


class _FakeClock:
    """Drives adapters.__main__.time.monotonic so an interval loop can be
    stepped deterministically without real sleeping."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_interval_loop_fetches_only_when_due(tmp_path, monkeypatch):
    db = _interval_db(tmp_path, seconds=10)
    clock = _FakeClock()
    monkeypatch.setattr(main_module.time, "monotonic", clock.monotonic)

    stop_event = threading.Event()
    calls = []

    def advancing_wait(self, timeout=None):
        clock.advance(timeout if timeout is not None else 0)
        return self.is_set()

    monkeypatch.setattr(threading.Event, "wait", advancing_wait)

    def fake_fetch_once(source, adapter):
        calls.append(clock.now)
        if len(calls) >= 3:
            stop_event.set()

    monkeypatch.setattr(main_module, "_fetch_once", fake_fetch_once)
    _run_adapter_loop("fake", stop_event, db_path=db)

    # fetch at t=0, then each subsequent fetch once >=10s has elapsed; idle
    # polls advance the clock in min(due_in, cap) steps.
    assert calls[0] == 0.0
    assert all(calls[i + 1] - calls[i] >= 10 for i in range(len(calls) - 1))
    assert calls[-1] <= 30  # not one big 10s jump per fetch, capped polling


def test_interval_loop_picks_up_a_shorter_policy_without_a_full_wait(tmp_path, monkeypatch):
    """The whole point: a source on 'every 1h' switched to 'every 20s' must
    fetch again within ~_LOOP_POLL_CAP_SECONDS, not after the hour."""
    db = _interval_db(tmp_path, seconds=3600)
    clock = _FakeClock()
    monkeypatch.setattr(main_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(threading.Event, "wait", lambda self, *a, **k: self.is_set())

    stop_event = threading.Event()
    calls = []

    def fake_fetch_once(source, adapter):
        calls.append(clock.now)
        if len(calls) == 1:
            conn = get_connection(db)
            set_policy(conn, "p", fetch_kind="interval", fetch_interval_seconds=20)
            conn.close()
        if len(calls) >= 2:
            stop_event.set()

    def advancing_wait(self, timeout=None):
        clock.advance(timeout if timeout is not None else 0)
        return self.is_set()

    monkeypatch.setattr(threading.Event, "wait", advancing_wait)
    monkeypatch.setattr(main_module, "_fetch_once", fake_fetch_once)
    _run_adapter_loop("fake", stop_event, db_path=db)

    assert calls[0] == 0.0
    # second fetch happens once >=20s elapsed — well under the old 3600s
    assert 20 <= calls[1] <= 20 + main_module._LOOP_POLL_CAP_SECONDS


def test_run_adapter_loop_rebuilds_adapter_each_cycle(tmp_path, monkeypatch):
    """A config edit is picked up without a restart — the loop re-reads the
    row every iteration."""
    db = _interval_db(tmp_path, seconds=10)
    clock = _FakeClock()
    monkeypatch.setattr(main_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(threading.Event, "wait",
                        lambda self, t=None: (clock.advance(t or 0), self.is_set())[1])

    stop_event = threading.Event()
    seen_urls = []

    def capture(source, adapter):
        seen_urls.append(adapter.config["url"])
        if len(seen_urls) == 1:
            conn = get_connection(db)
            set_adapter_instance(conn, "fake", "api", {"url": "https://EDITED"}, policy="p")
            conn.close()
        if len(seen_urls) >= 2:
            stop_event.set()

    monkeypatch.setattr(main_module, "_fetch_once", capture)
    _run_adapter_loop("fake", stop_event, db_path=db)

    assert seen_urls == ["https://example.test", "https://EDITED"]


def test_run_adapter_loop_stops_when_row_removed(tmp_path, monkeypatch):
    db = _interval_db(tmp_path, seconds=1)
    stop_event = threading.Event()

    def remove_then_noop(source, adapter):
        conn = get_connection(db)
        conn.execute("DELETE FROM adapter_instances WHERE source = 'fake'")
        conn.commit()
        conn.close()

    monkeypatch.setattr(threading.Event, "wait", lambda self, *a, **k: self.is_set())
    monkeypatch.setattr(main_module, "_fetch_once", remove_then_noop)

    _run_adapter_loop("fake", stop_event, db_path=db)


def _wait_until(pred, timeout=3.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def test_supervisor_spawns_loop_for_a_source_added_after_start(tmp_path, monkeypatch):
    """A source enabled while the adapters process is already running gets a
    loop thread on the next supervisor scan — no restart."""
    db = tmp_path / "radiobeacon.db"
    get_connection(db).close()
    monkeypatch.setattr(main_module, "_SUPERVISOR_POLL_SECONDS", 0.05)

    started: list[str] = []

    def fake_loop(source, stop_event, *, db_path=None):
        started.append(source)
        stop_event.wait()

    monkeypatch.setattr(main_module, "_run_adapter_loop", fake_loop)

    stop_event = threading.Event()
    sup = threading.Thread(target=main_module._supervise, args=(stop_event,), kwargs={"db_path": db}, daemon=True)
    sup.start()
    try:
        # the seeded csn/senapred spin up first; "late" doesn't exist yet
        _wait_until(lambda: set(started) == {"csn", "senapred"})
        assert "late" not in started

        conn = get_connection(db)
        set_adapter_instance(conn, "late", "api", {"url": "https://x.test"})
        conn.close()
        _wait_until(lambda: "late" in started)
    finally:
        stop_event.set()
        sup.join(timeout=2)


def test_supervisor_does_not_double_spawn(tmp_path, monkeypatch):
    db = tmp_path / "radiobeacon.db"
    conn = get_connection(db)
    set_adapter_instance(conn, "s1", "api", {"url": "https://x.test"})
    conn.close()
    monkeypatch.setattr(main_module, "_SUPERVISOR_POLL_SECONDS", 0.05)

    starts: list[str] = []

    def fake_loop(source, stop_event, *, db_path=None):
        starts.append(source)
        stop_event.wait()

    monkeypatch.setattr(main_module, "_run_adapter_loop", fake_loop)

    stop_event = threading.Event()
    sup = threading.Thread(target=main_module._supervise, args=(stop_event,), kwargs={"db_path": db}, daemon=True)
    sup.start()
    try:
        _wait_until(lambda: "s1" in starts)
        import time
        time.sleep(0.3)  # several supervisor scans
        assert starts.count("s1") == 1  # not re-spawned
    finally:
        stop_event.set()
        sup.join(timeout=2)
