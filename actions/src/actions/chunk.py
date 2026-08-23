import logging
import re
import sqlite3
import textwrap
from typing import Any

from adapters.ax25 import max_frame_content_bytes
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


def _effective_max_chars(conn: sqlite3.Connection, configured_max_chars: int) -> int:
    """Clamps ACTIONS_CHUNK_MAX_CHARS down to whatever actually fits in
    one AX.25 frame given the CURRENT BEACON_CALLSIGN/BEACON_FRAME_DESTINATION/
    BEACON_FRAME_PREFIX/BEACON_FRAME_SUFFIX — those all eat into the same
    256-byte budget beacon.formatters.format_frame enforces at transmit
    time, and unlike the static default, they can change (a longer
    suffix, a longer callsign) without this ceiling following along. The
    configured value stays a preference/ceiling, never raised — only
    lowered when it would otherwise risk an assembled frame overflowing
    and getting silently dropped (beacon.frame.dropped_too_long).

    BEACON_CALLSIGN unset (beacon not configured yet — a real, common
    early-lifecycle state) means there's nothing to clamp against yet, so
    this is a no-op in that case, preserving prior behavior exactly."""
    callsign = get_setting("BEACON_CALLSIGN", conn=conn, env_fallback=False)
    if not callsign:
        return configured_max_chars
    destination = get_setting("BEACON_FRAME_DESTINATION", "WXALRT", conn=conn)
    prefix = get_setting("BEACON_FRAME_PREFIX", "", conn=conn) or ""
    suffix = get_setting("BEACON_FRAME_SUFFIX", "", conn=conn) or ""
    available = max(
        1,
        max_frame_content_bytes(callsign=callsign, destination=destination, prefix=prefix, suffix=suffix),
    )
    if available < configured_max_chars:
        logger.warning(
            "chunk: ACTIONS_CHUNK_MAX_CHARS=%d would risk AX.25 frame overflow given current "
            "beacon callsign/destination/prefix/suffix (%d bytes available) — clamping to %d",
            configured_max_chars, available, available,
        )
    return min(configured_max_chars, available)


class ChunkAction(Action):
    """Subscribes to actions.ai's output (item.ai_settled by default), not
    item.dispatched directly — so this always runs AFTER ai has settled
    for the same dispatch, whether or not it actually produced a summary
    (ai always publishes — see ai.py's own docstring for why). Looks up
    the item's summary (preferred) or extracted_contents (fallback,
    mirroring beacon.content.resolve_voice_text's exact pattern) and
    splits it into small, word-boundary-safe chunks, durably stored (in
    order) in the `chunks` table — queryable via
    `query_history.sh chunks <source> <item_id>`. Only once every chunk
    is stored does run() return, and __main__.py publishes a single
    `item.chunked` CloudEvent as a "chunks are ready, go query them"
    pointer — not a payload carrier — for actions.content_ready and,
    downstream of that, beacon to consume."""

    def run(self, event: dict[str, Any], *, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        data = event.get("data") or {}
        source, item_id = data.get("source"), data.get("item_id")
        if not source or not item_id:
            logger.warning("chunk: event missing source/item_id, skipping: %r", data)
            return []

        row = conn.execute(
            "SELECT summary, extracted_contents FROM items WHERE source = ? AND item_id = ?",
            (source, item_id),
        ).fetchone()
        summary, extracted_contents = row if row else (None, None)
        contents = summary if summary else extracted_contents
        if not contents:
            logger.info(
                "chunk: source=%s item_id=%s has no summary or extracted_contents, skipping",
                source,
                item_id,
            )
            return []

        contents = _normalize_unicode_escapes(contents)

        configured_max_chars = int(get_setting("ACTIONS_CHUNK_MAX_CHARS", "200", conn=conn))
        max_chars = _effective_max_chars(conn, configured_max_chars)
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
