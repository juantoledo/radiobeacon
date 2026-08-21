import sqlite3
from abc import ABC, abstractmethod
from typing import Any


class Action(ABC):
    @abstractmethod
    def run(self, event: dict[str, Any], *, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """`event` is the full parsed CloudEvent envelope as a dict
        (specversion, type, source, id, time, data, ...) — not just
        `data` — so an action subscribed to more than one topic can
        branch on event["type"]. `conn` is a fresh sqlite3 connection the
        runner opens for this one message and closes afterward (mirrors
        DataSourceAdapter.fetch_and_store's per-call
        get_connection()/close() in adapters/base.py).

        Returns zero or more output payloads — each dict becomes one
        CloudEvent's `data` field, published to this action's configured
        output topic (see __main__.py). Return [] for "nothing to
        publish this time" (a normal, non-error outcome) or when this
        action has no OUTPUT_TOPIC configured at all (a terminal
        action)."""
        ...
