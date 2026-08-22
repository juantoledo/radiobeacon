import logging
import re
import sqlite3
import textwrap
from typing import Any

from adapters.storage import get_setting, store_chunks

from actions.base import Action

logger = logging.getLogger(__name__)

# Matches only literal 4-hex-digit \uXXXX escape sequences (e.g. "ó"
# appearing as six literal characters in the text, not a real ó) — a
# narrow, targeted pattern rather than decoding via the `unicode_escape`
# codec, which also reinterprets other backslash sequences (\n, \\, ...)
# and can corrupt text that has genuine literal backslashes.
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _normalize_unicode_escapes(text: str) -> str:
    """Some sources round-trip through JSON more than once upstream,
    leaving escape sequences like "\\u00f3" as literal text instead of
    the accented character they represent (e.g. "informaci\\u00f3n"
    instead of "información"). Decodes those in place so chunked text
    reads correctly instead of leaking raw escape codes downstream."""
    return _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


class ChunkAction(Action):
    """On an item.dispatched-shaped event, looks up the item's
    extracted_contents in the items table and splits it into small,
    word-boundary-safe chunks, durably stored (in order) in the `chunks`
    table — queryable via `query_history.sh chunks <source> <item_id>`.
    Only once every chunk is stored does run() return, and __main__.py
    publishes a single `item.chunked` CloudEvent as a "chunks are ready,
    go query them" pointer — not a payload carrier — for a future
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

        contents = _normalize_unicode_escapes(contents)

        max_chars = int(get_setting("ACTIONS_CHUNK_MAX_CHARS", "200", conn=conn))
        pieces = textwrap.wrap(
            contents, width=max_chars, break_long_words=False, break_on_hyphens=False
        )
        logger.info(
            "chunk: source=%s item_id=%s split into %d chunk(s) (max_chars=%d)",
            source,
            item_id,
            len(pieces),
            max_chars,
        )
        for i, piece in enumerate(pieces):
            logger.info(
                "chunk: source=%s item_id=%s chunk %d/%d: %r",
                source,
                item_id,
                i + 1,
                len(pieces),
                piece,
            )

        chunk_rows = [
            {
                "source": source,
                "item_id": item_id,
                "chunk_index": i,
                "chunk_count": len(pieces),
                "text": piece,
            }
            for i, piece in enumerate(pieces)
        ]
        store_chunks(conn, chunk_rows)  # every chunk persisted, in order, before any event fires

        return [{"source": source, "item_id": item_id, "chunk_count": len(pieces)}]
