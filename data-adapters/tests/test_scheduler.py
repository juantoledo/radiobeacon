import threading
from dataclasses import dataclass
from datetime import datetime

from adapters.__main__ import _run_adapter_loop
from adapters.base import DataSourceAdapter, SourceReading


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


def test_run_adapter_loop_calls_fetch_and_store_until_stopped(monkeypatch):
    stop_event = threading.Event()
    calls = []

    def fake_fetch_and_store(self, *args, **kwargs):
        calls.append(1)
        if len(calls) >= 3:
            stop_event.set()
        return SourceReading(source="fake", fetched_at=datetime.now(), ok=True, data=[])

    monkeypatch.setattr(FakeAdapter, "fetch_and_store", fake_fetch_and_store)

    _run_adapter_loop("fake", FakeAdapter(), 0, stop_event)

    assert len(calls) == 3


def test_run_adapter_loop_continues_after_exception(monkeypatch):
    stop_event = threading.Event()
    calls = []

    def flaky_fetch_and_store(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop_event.set()
        return SourceReading(source="fake", fetched_at=datetime.now(), ok=True, data=[])

    monkeypatch.setattr(FakeAdapter, "fetch_and_store", flaky_fetch_and_store)

    _run_adapter_loop("fake", FakeAdapter(), 0, stop_event)

    assert len(calls) == 2
