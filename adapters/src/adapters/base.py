import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .storage import DEFAULT_DB_PATH, get_connection, record_audit_event, store_reading

logger = logging.getLogger(__name__)


@dataclass
class SourceReading:
    source: str
    fetched_at: datetime
    ok: bool
    data: Any
    error: str | None = None


class DataSourceAdapter(ABC):
    @abstractmethod
    def fetch(self) -> SourceReading:
        ...

    def fetch_and_store(self, db_path: str | Path = DEFAULT_DB_PATH) -> SourceReading:
        """Part of the adapter contract: every fetch is captured to the
        SQLite store, so callers don't have to remember to persist it.
        Storing raw data only — summarization is enrichment's job, run
        separately (see enrichment/summarize_item.py)."""
        logger.info("%s: fetching", type(self).__name__)
        reading = self.fetch()
        if not reading.ok:
            logger.warning(
                "%s: fetch failed: %s", type(self).__name__, reading.error
            )
        conn = get_connection(db_path)
        try:
            stored_count = store_reading(conn, reading)
            record_audit_event(
                conn,
                event_type="adapter.fetch",
                actor=f"adapters.{type(self).__name__}",
                source=reading.source,
                details={"ok": reading.ok, "error": reading.error, "stored_count": stored_count},
            )
        finally:
            conn.close()
        return reading
