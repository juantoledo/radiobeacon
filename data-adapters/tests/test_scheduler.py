import threading
from dataclasses import dataclass
from datetime import datetime

from adapters.__main__ import _run_adapter_loop
from adapters.base import DataSourceAdapter, SourceReading
from adapters.policy import set_policy
from adapters.storage import get_connection, set_adapter_instance


@dataclass
class FakeItem:
    id: str


class FakeAdapter(DataSourceAdapter):
    def fetch(self) -> SourceReading:
        return SourceReading(
            source="fake",
            fetched_at=datetime(2026, 8, 19, 12, 0, 0),
            ok=True,
            data=[FakeItem(id="1")],
        )


def _fast_interval_db(tmp_path):
    """A DB with a `fake` adapter instance on a 1s-interval Policy."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_policy(conn, "fast", fetch_kind="interval", fetch_interval_seconds=1)
    set_adapter_instance(conn, "fake", "api", {"url": "https://example.test"}, policy="fast")
    conn.close()
    return tmp_path / "radiobeacon.db"


def test_run_adapter_loop_calls_fetch_and_store_until_stopped(tmp_path, monkeypatch):
    db = _fast_interval_db(tmp_path)
    stop_event = threading.Event()
    calls = []

    def fake_fetch_and_store(self, *args, **kwargs):
        calls.append(1)
        if len(calls) >= 3:
            stop_event.set()
        return SourceReading(source="fake", fetched_at=datetime.now(), ok=True, data=[])

    # a no-op wait so the 1s interval doesn't slow the test
    monkeypatch.setattr(threading.Event, "wait", lambda self, *a, **k: self.is_set())
    monkeypatch.setattr(FakeAdapter, "fetch_and_store", fake_fetch_and_store)

    _run_adapter_loop("fake", FakeAdapter(), stop_event, db_path=db)

    assert len(calls) == 3


def test_run_adapter_loop_continues_after_exception(tmp_path, monkeypatch):
    db = _fast_interval_db(tmp_path)
    stop_event = threading.Event()
    calls = []

    def flaky_fetch_and_store(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop_event.set()
        return SourceReading(source="fake", fetched_at=datetime.now(), ok=True, data=[])

    monkeypatch.setattr(threading.Event, "wait", lambda self, *a, **k: self.is_set())
    monkeypatch.setattr(FakeAdapter, "fetch_and_store", flaky_fetch_and_store)

    _run_adapter_loop("fake", FakeAdapter(), stop_event, db_path=db)

    assert len(calls) == 2
