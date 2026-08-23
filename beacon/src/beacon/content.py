"""Lazy content resolution — the queue holds lightweight references, not
pre-rendered text; the actual text is read fresh from the DB at transmit
time, not at enqueue time.

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

Also: actions.ai structurally skips summarization for content already at
or under ACTIONS_AI_MAX_CHARS ("nothing meaningful to condense") — CSN's
own `contents` (a short templated string) is almost always under that
threshold, so CSN items never produce a summary regardless of whether AI
is enabled. resolve_voice_text's summary-else-extracted_contents fallback
is what keeps CSN (and any other structurally-short source) voice-able."""
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class QueuedFrame:
    source: str
    item_id: str
    chunk_index: int


@dataclass(frozen=True)
class QueuedVoice:
    source: str
    item_id: str


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
    """items.summary if AI has produced one by now, else
    items.extracted_contents (the CSN/AI-disabled fallback). None if the
    item itself is gone, or has no content either way."""
    row = conn.execute(
        "SELECT summary, extracted_contents FROM items WHERE source = ? AND item_id = ?",
        (source, item_id),
    ).fetchone()
    if row is None:
        return None
    summary, extracted_contents = row
    return summary if summary else extracted_contents
