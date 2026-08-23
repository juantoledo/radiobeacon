import logging
import re
import sqlite3
import textwrap
from datetime import datetime, timezone
from typing import Any

from adapters.ax25 import max_frame_content_bytes
from adapters.storage import get_setting, store_chunks

from actions.base import Action

logger = logging.getLogger(__name__)

# A fixed, representative datetime used only to measure how many bytes a
# {date}-containing BEACON_FRAME_PREFIX/SUFFIX template would actually
# render to (see _effective_max_chars) -- never used for anything else.
# Safe as long as BEACON_DATE_FORMAT sticks to fixed-width numeric
# directives (%d/%m/%Y/%H/%M are always 2 or 4 digits); a format using a
# weekday/month name (%A/%B) would vary in width and isn't accounted for.
_SAMPLE_DATE = datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)

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


def _wrap_with_part_markers(contents: str, max_chars: int) -> list[str]:
    """Word-boundary-safe split (textwrap.wrap), then -- only when it
    actually produced more than one piece -- suffixes each with a 1-based
    " i/n" part marker (e.g. " 1/2", " 2/2") baked directly into the
    stored chunk text, so a listener catching just one AX.25 frame out of
    several knows its place in the sequence. A single-piece result is
    left unmarked ("1/1" would be pure noise). Distinct from the stored
    chunk_index/chunk_count columns (0-based, for internal lookup) --
    this marker is purely a human-readable addition to the transmitted
    content itself.

    The marker eats into max_chars, so pieces are wrapped a second time
    at a narrower width reserving room for it -- the marker's own width
    depends on the piece count, which isn't known until after an
    unmarked first pass. In the rare case where narrowing pushes the
    piece count past a digit-width boundary (e.g. 9 -> 10), the reserved
    width could be a character or two short; formatters.format_frame's
    own byte-exact check at transmit time remains the real backstop, same
    as the multi-byte-accented-character edge case it already covers."""
    unmarked = textwrap.wrap(contents, width=max_chars, break_long_words=False, break_on_hyphens=False)
    if len(unmarked) <= 1:
        return unmarked

    marker_width = len(f" {len(unmarked)}/{len(unmarked)}")
    narrowed_width = max(1, max_chars - marker_width)
    pieces = textwrap.wrap(contents, width=narrowed_width, break_long_words=False, break_on_hyphens=False)
    if len(pieces) <= 1:
        return pieces

    total = len(pieces)
    return [f"{piece} {i}/{total}" for i, piece in enumerate(pieces, start=1)]


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
    this is a no-op in that case, preserving prior behavior exactly.

    BEACON_FRAME_PREFIX/SUFFIX are str.format templates, not plain
    literals (see beacon.formatters.format_frame) — a short template like
    " {date}" can render to something much longer once BEACON_DATE_FORMAT
    is applied. Measuring the raw, unrendered template here would
    silently underestimate real overhead and reopen the exact overflow
    risk this clamp exists to prevent, so both are rendered against a
    fixed sample date before their length is measured."""
    callsign = get_setting("BEACON_CALLSIGN", conn=conn, env_fallback=False)
    if not callsign:
        return configured_max_chars
    destination = get_setting("BEACON_FRAME_DESTINATION", "WXALRT", conn=conn)
    date_format = get_setting("BEACON_DATE_FORMAT", "%d-%m-%Y %H:%M", conn=conn)
    sample_date = _SAMPLE_DATE.strftime(date_format)
    prefix = (get_setting("BEACON_FRAME_PREFIX", "", conn=conn) or "").format(date=sample_date)
    suffix = (get_setting("BEACON_FRAME_SUFFIX", "", conn=conn) or "").format(date=sample_date)
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
    splits it into small, word-boundary-safe chunks (each one suffixed
    with a " i/n" part marker when there's more than one — see
    _wrap_with_part_markers), durably stored (in order) in the `chunks`
    table — queryable via
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
        pieces = _wrap_with_part_markers(contents, max_chars)
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
