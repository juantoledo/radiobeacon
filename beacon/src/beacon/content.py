"""Lazy content resolution — beacon_tx_schedule rows hold lightweight
references (source, item_id, kind, ref), not pre-rendered text; the actual
text is read fresh from the DB at transmit time, not at schedule time.

Why lazy: actions.ai runs concurrently with beacon's own MQTT subscriber
(both react to item.dispatched-adjacent events independently), so a
summary genuinely may not exist yet at enqueue time even when AI is
enabled and will eventually produce one. Deferring the read to transmit
time — which, given typical TDMA window lengths of tens of seconds to
minutes, is almost always well after a same-item AI call would have
completed — avoids ever needing to guess "is a summary coming or not."
It also means a later rearm, or the item's summary changing between
enqueue and transmit, is naturally reflected: whatever's true right now
is what gets sent.

Also: actions.ai ALWAYS populates items.summary once an item has
extracted_contents — copying extracted_contents in verbatim when there's
nothing meaningful to condense (AI disabled, content already at or under
ACTIONS_AI_MAX_CHARS, bad provider config) rather than leaving summary
NULL (see ai.py). resolve_voice_text can therefore read items.summary
unconditionally, with no extracted_contents fallback of its own.

resolve_item_fields resolves the same way (fresh, at transmit time) --
type/subtype/extracted_title/url, the item-derived placeholders available
to both channels' prefix/suffix (and voice's template) beyond {date} --
plus source_name/source_url, which aren't item data at all but the
current source's own display metadata (adapters.storage.sources,
seeded with csn/senapred, editable via data-adapters/sources.sh)."""
import sqlite3
from datetime import datetime

from adapters.storage import get_source_fields


def resolve_frame_text(conn: sqlite3.Connection, source: str, item_id: str, chunk_index: int) -> str | None:
    """Re-reads one specific chunks row fresh. None if it's since been
    deleted (e.g. by a /dev item delete) — the caller treats that as
    "nothing to send", not an error. actions.chunk now runs after
    actions.ai and chunks the summary when one exists (falling back to
    extracted_contents otherwise — see chunk.py), so this row already
    reflects the best available content; no separate summary-vs-chunk
    branch is needed here."""
    row = conn.execute(
        "SELECT text FROM chunks WHERE source = ? AND item_id = ? AND chunk_index = ?",
        (source, item_id, chunk_index),
    ).fetchone()
    return row[0] if row is not None else None


def resolve_voice_text(conn: sqlite3.Connection, source: str, item_id: str) -> str | None:
    """items.summary, read unconditionally — actions.ai guarantees it's
    populated (a real summary, or extracted_contents copied in verbatim)
    by the time item.content_ready fires. None if the item itself is
    gone, or summary is still NULL (e.g. an item from before this
    guarantee existed, never reprocessed)."""
    row = conn.execute(
        "SELECT summary FROM items WHERE source = ? AND item_id = ?",
        (source, item_id),
    ).fetchone()
    return row[0] if row is not None else None


def resolve_item_fields(conn: sqlite3.Connection, source: str, item_id: str) -> dict[str, str]:
    """type/subtype/extracted_title/url/source_name/source_url for
    BEACON_FRAME_PREFIX/SUFFIX and BEACON_VOICE_PREFIX/SUFFIX/TEMPLATE
    placeholders -- source/item_id aren't queried here since the caller
    already has them. Empty strings, never None, so a template
    referencing e.g. {url} on an item with no url renders "" rather than
    the literal string "None"; all-empty (not an error) if the item is
    gone by transmit time -- mirrors resolve_frame_text/resolve_voice_text's
    "nothing to send, not an error" treatment of a vanished item.

    source_name/source_url come from adapters.storage.get_source_fields
    (the `sources` table), not from this item's own row -- they're the
    same for every item sharing this source, and fail-soft to the raw
    source key / "" for a source with no `sources` row of its own."""
    row = conn.execute(
        "SELECT type, subtype, extracted_title, url FROM items WHERE source = ? AND item_id = ?",
        (source, item_id),
    ).fetchone()
    item_type, subtype, extracted_title, url = row if row is not None else (None, None, None, None)
    fields = {
        "type": item_type or "",
        "subtype": subtype or "",
        "extracted_title": extracted_title or "",
        "url": url or "",
    }
    fields.update(get_source_fields(conn, source))
    return fields


def resolve_source_date_time(conn: sqlite3.Connection, source: str, item_id: str) -> datetime | None:
    """items.source_date_time (stored as TEXT via .isoformat(), always
    UTC per this repo's "always UTC" storage rule) parsed back to a
    UTC-aware datetime. None if the item is gone, or the column is
    somehow NULL — a graceful "no date available" case for the caller,
    not an error."""
    row = conn.execute(
        "SELECT source_date_time FROM items WHERE source = ? AND item_id = ?",
        (source, item_id),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return datetime.fromisoformat(row[0])
