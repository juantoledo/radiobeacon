import sqlite3
from dataclasses import dataclass
from datetime import datetime

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


def test_fetch_and_store_persists_the_reading(tmp_path):
    db_path = tmp_path / "radiobeacon.db"

    reading = FakeAdapter().fetch_and_store(db_path=db_path)

    assert reading.source == "fake"

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert count == 1


def test_fetch_and_store_records_an_adapter_fetch_audit_event(tmp_path):
    db_path = tmp_path / "radiobeacon.db"

    FakeAdapter().fetch_and_store(db_path=db_path)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT event_type, actor, source, details FROM audit_log WHERE event_type = 'adapter.fetch'"
    ).fetchone()
    assert row[0] == "adapter.fetch"
    assert row[1] == "adapters.FakeAdapter"
    assert row[2] == "fake"
    assert '"stored_count": 1' in row[3]
