import logging
import os
import sqlite3
import textwrap
from typing import Any

from actions.base import Action

logger = logging.getLogger(__name__)


class ChunkAction(Action):
    """On an item.dispatched-shaped event, looks up the item's
    extracted_contents in the items table and splits it into small,
    word-boundary-safe chunks, published (by __main__.py) one CloudEvent
    per chunk to this action's configured output topic — for a future
    downstream action (e.g. an AX.25 formatter) to consume."""

    def run(self, event: dict[str, Any], *, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        data = event.get("data") or {}
        source, item_id = data.get("source"), data.get("item_id")
        if not source or not item_id:
            logger.warning("chunk: event missing source/item_id, skipping: %r", data)
            return []

        row = conn.execute(
            "SELECT extracted_contents FROM items WHERE source = ? AND item_id = ?",
            (source, item_id),
        ).fetchone()
        contents = row[0] if row else None
        if not contents:
            logger.info(
                "chunk: source=%s item_id=%s has no extracted_contents, skipping",
                source,
                item_id,
            )
            return []

        max_chars = int(os.environ.get("ACTIONS_CHUNK_MAX_CHARS", "200"))
        pieces = textwrap.wrap(
            contents, width=max_chars, break_long_words=False, break_on_hyphens=False
        )
        return [
            {
                "source": source,
                "item_id": item_id,
                "chunk_index": i,
                "chunk_count": len(pieces),
                "text": piece,
            }
            for i, piece in enumerate(pieces)
        ]
