import threading
from dataclasses import dataclass
from datetime import datetime

from adapters.__main__ import _interval_seconds, _run_adapter_loop
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


# _interval_seconds derives the env var from the adapter's module name —
# fake a module path so it doesn't collide with any real adapter's vars.
FakeAdapter.__module__ = "adapters.fakesource"


def test_interval_seconds_uses_adapter_specific_env_var(monkeypatch):
    monkeypatch.setenv("ADAPTERS_FAKESOURCE_INTERVAL_SECONDS", "30")
    monkeypatch.delenv("ADAPTERS_DEFAULT_INTERVAL_SECONDS", raising=False)

    assert _interval_seconds(FakeAdapter) == 30


def test_interval_seconds_falls_back_to_package_default(monkeypatch):
    monkeypatch.delenv("ADAPTERS_FAKESOURCE_INTERVAL_SECONDS", raising=False)
    monkeypatch.setenv("ADAPTERS_DEFAULT_INTERVAL_SECONDS", "45")

    assert _interval_seconds(FakeAdapter) == 45


def test_interval_seconds_falls_back_to_hardcoded_default(monkeypatch):
    monkeypatch.delenv("ADAPTERS_FAKESOURCE_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("ADAPTERS_DEFAULT_INTERVAL_SECONDS", raising=False)

    assert _interval_seconds(FakeAdapter) == 10


def test_run_adapter_loop_calls_fetch_and_store_until_stopped(monkeypatch):
    monkeypatch.setenv("ADAPTERS_FAKESOURCE_INTERVAL_SECONDS", "0")
    stop_event = threading.Event()
    calls = []

    def fake_fetch_and_store(self, *args, **kwargs):
        calls.append(1)
        if len(calls) >= 3:
            stop_event.set()
        return SourceReading(
            source="fake", fetched_at=datetime.now(), ok=True, data=[]
        )

    monkeypatch.setattr(FakeAdapter, "fetch_and_store", fake_fetch_and_store)

    _run_adapter_loop(FakeAdapter, stop_event)

    assert len(calls) == 3


def test_run_adapter_loop_continues_after_exception(monkeypatch):
    monkeypatch.setenv("ADAPTERS_FAKESOURCE_INTERVAL_SECONDS", "0")
    stop_event = threading.Event()
    calls = []

    def flaky_fetch_and_store(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop_event.set()
        return SourceReading(
            source="fake", fetched_at=datetime.now(), ok=True, data=[]
        )

    monkeypatch.setattr(FakeAdapter, "fetch_and_store", flaky_fetch_and_store)

    _run_adapter_loop(FakeAdapter, stop_event)

    assert len(calls) == 2
