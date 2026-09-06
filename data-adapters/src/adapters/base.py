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


@dataclass
class AdapterItem:
    """Formalizes the item-side contract adapters.storage.store_reading has
    always duck-typed via getattr(item, name, None): every generic adapter
    (ApiAdapter, CustomAdapter) builds SourceReading.data as a list of these,
    replacing the old bespoke per-adapter dataclasses (CsnEarthquake,
    SenapredAlert) whose field-computation logic now lives in config (API
    type) or the stored snippet (CUSTOM type) instead of Python code."""

    id: str
    title: str | None = None
    contents: str | None = None
    url: str | None = None
    event_key: str | None = None
    type: str | None = None
    subtype: str | None = None
    policy: str | None = None
    source_date_time: datetime | None = None
    # The original, unmapped item as returned by the source (raw JSON dict
    # for ApiAdapter, whatever dict the snippet's own source data came from
    # for CustomAdapter) — preserved here so `rawdata` (store_reading dumps
    # this whole dataclass via dataclasses.asdict) keeps carrying the true
    # raw payload alongside the mapped contract fields, not just the latter.
    raw: dict[str, Any] | None = None


class DataSourceAdapter(ABC):
    @abstractmethod
    def fetch(self) -> SourceReading:
        ...

    def fetch_and_store(self, db_path: str | Path = DEFAULT_DB_PATH) -> SourceReading:
        """Part of the adapter contract: every fetch is captured to the
        SQLite store, so callers don't have to remember to persist it.
        Storing raw data only."""
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
