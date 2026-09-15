"""Locating the WAV clips the beacon renders (per-item voice bulletins and
one-shot manual transmissions) so the dashboard can play them back in the
browser.

The beacon writes one WAV per voice unit into BEACON_TTS_WAV_DIR, named
``{source}-{item_id}-{unix_ts}.wav`` (frame/packet clips get an extra
``-{chunk_index}`` segment and are *not* offered for playback — they're
AFSK modem tones, not speech). beacon.__main__ anchors a relative
BEACON_TTS_WAV_DIR at the repo root, the same top-level storage/
directory the UI reads from, so both processes see one shared folder.
Nothing here ever opens a caller-supplied path — every returned Path
comes from iterating the directory itself.
"""
import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from adapters.storage import get_setting

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Remainder after the ``{source}-{item_id}-`` prefix for a voice clip: just
# the unix timestamp. A frame clip's remainder is ``{chunk_index}-{ts}``,
# which this rejects.
_VOICE_REMAINDER = re.compile(r"\d+\.wav\Z")

# Manual one-shot clips: ``manual-{beacon_manual_tx.id}-{unix_ts}.wav`` (see
# beacon.__main__._transmit_manual_unit). Both voice and frame manual sends
# use this name — the file alone can't say which, so the dashboard just
# offers every one for playback.
_MANUAL_CLIP = re.compile(r"\Amanual-(\d+)-(\d+)\.wav\Z")


def wav_dir(conn: sqlite3.Connection) -> Path:
    """The directory the beacon renders TTS clips into — same resolution
    rule as beacon.__main__._resolve_wav_dir (relative -> repo root)."""
    raw = get_setting("BEACON_TTS_WAV_DIR", "storage/beacon_tts", conn=conn)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else _REPO_ROOT / path


def _safe_segment(value: str) -> bool:
    return bool(value) and not (set(value) & {"/", "\\", "\0"}) and ".." not in value


def latest_voice_clip(conn: sqlite3.Connection, source: str, item_id: str) -> Path | None:
    """Newest voice WAV the beacon rendered for this item, or None."""
    if not _safe_segment(source) or not _safe_segment(item_id):
        return None
    prefix = f"{source}-{item_id}-"
    try:
        candidates = [
            entry
            for entry in wav_dir(conn).iterdir()
            if entry.is_file()
            and entry.name.startswith(prefix)
            and _VOICE_REMAINDER.match(entry.name[len(prefix):])
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def manual_tx_audio_url(conn: sqlite3.Connection, event_type: str, details_raw: str | None) -> str | None:
    """The /dashboard/manual-audio/ playback URL for a beacon.manual.transmitted
    audit row's clip, so the dashboard's single activity feed can offer a
    play button on that row directly -- instead of a separate file-listing
    panel. None for every other event type, or once the clip has been
    removed from disk (pruned, or never rendered)."""
    if event_type != "beacon.manual.transmitted":
        return None
    try:
        details = json.loads(details_raw) if details_raw else {}
    except (TypeError, ValueError):
        return None
    name = details.get("clip")
    if not isinstance(name, str) or manual_clip_path(conn, name) is None:
        return None
    return f"/dashboard/manual-audio/{name}"


# Successful-transmission audit events that carry a `clip` filename in their
# details JSON (added by beacon.__main__ — see _transmit_voice_unit /
# _transmit_frame_unit). One row per airing, so a repeating Policy produces
# several; each has its own WAV.
_CLIP_EVENT_KIND = {
    "beacon.voice.transmitted": "voice",
    "beacon.frame.transmitted": "frame",
}

# A clip name that legitimately belongs to (source, item_id): the beacon
# builds it as `{source}-{item_id}-{ts}.wav` (voice) or
# `{source}-{item_id}-{chunk}-{ts}.wav` (frame).
#
# lru_cache, not a fresh re.compile() every call: both call sites
# (list_item_transmissions, item_clip_path) already run _safe_segment on
# source/item_id first, so by the time we get here the values are bounded,
# separator-free strings — safe to key a *bounded* cache on. maxsize=512
# rather than unbounded because item_id ultimately comes from adapter-
# supplied data (externally influenced), so we still cap how many distinct
# patterns we'll hold onto.
@lru_cache(maxsize=512)
def _item_clip_re(source: str, item_id: str) -> re.Pattern:
    return re.compile(rf"\A{re.escape(source)}-{re.escape(item_id)}-(?:(\d+)-)?\d+\.wav\Z")


def list_item_transmissions(
    conn: sqlite3.Connection, source: str, item_id: str
) -> list[dict]:
    """Every transmission this item has had that still has its rendered WAV
    on disk, newest first. Read straight from `audit_log` (authoritative air
    time + metadata), filtered to rows whose `clip` file still exists.

    Each entry: ``{"kind": "voice"|"frame", "ref": int|None, "at": <iso>,
    "name": <filename>, "url": "/items/<s>/<i>/audio/<filename>",
    "truncated": bool|None, "byte_length": int|None}``.
    """
    if not _safe_segment(source) or not _safe_segment(item_id):
        return []
    rows = conn.execute(
        "SELECT event_type, details, recorded_at FROM audit_log "
        "WHERE source = ? AND item_id = ? "
        "AND event_type IN ('beacon.voice.transmitted', 'beacon.frame.transmitted') "
        "ORDER BY id DESC",
        (source, item_id),
    ).fetchall()
    directory = wav_dir(conn)
    name_re = _item_clip_re(source, item_id)
    out: list[dict] = []
    for event_type, details_raw, recorded_at in rows:
        try:
            details = json.loads(details_raw) if details_raw else {}
        except (TypeError, ValueError):
            details = {}
        name = details.get("clip")
        if not isinstance(name, str):
            continue
        match = name_re.match(name)
        if match is None:
            continue
        if not (directory / name).is_file():
            continue
        ref = details.get("ref")
        if ref is None and match.group(1) is not None:
            ref = int(match.group(1))
        out.append(
            {
                "kind": _CLIP_EVENT_KIND[event_type],
                "ref": ref,
                "at": recorded_at,
                "name": name,
                "url": f"/items/{source}/{item_id}/audio/{name}",
                "truncated": details.get("truncated"),
                "byte_length": details.get("byte_length"),
            }
        )
    return out


def item_transmission_counts(
    conn: sqlite3.Connection, items: list[sqlite3.Row]
) -> dict[tuple[str, str], int]:
    """{(source, item_id): count} of successful transmissions per item — one
    grouped query over `audit_log` for the dashboard feed (decides which
    rows get a play button / an "xN" disclosure). Not file-existence
    filtered: this is a cheap "has it ever aired?" hint; the per-item list
    (list_item_transmissions) does the disk check."""
    keys = sorted({(row["source"], row["item_id"]) for row in items})
    if not keys:
        return {}
    pairs = ",".join("(?,?)" for _ in keys)
    rows = conn.execute(
        # `+event_type`: without the unary +, SQLite may drive this off an
        # event_type index and still scan every transmit row ever recorded
        # before filtering to these specific items; + disqualifies
        # event_type from index selection so the planner uses the
        # (source, item_id) index instead and only touches these items'
        # rows.
        f"SELECT source, item_id, COUNT(*) FROM audit_log "
        f"WHERE (source, item_id) IN (VALUES {pairs}) "
        f"AND +event_type IN ('beacon.voice.transmitted', 'beacon.frame.transmitted') "
        f"GROUP BY source, item_id",
        [v for pair in keys for v in pair],
    ).fetchall()
    return {(s, i): c for s, i, c in rows}


def item_clip_path(
    conn: sqlite3.Connection, source: str, item_id: str, name: str
) -> Path | None:
    """Resolve one clip `name` (from list_item_transmissions) to a file on
    disk, or None. Rejects any name that doesn't match this item's own
    `{source}-{item_id}-[chunk-]{ts}.wav` shape, so a path segment can't
    escape the wav dir or reach another item's clips."""
    if not _safe_segment(source) or not _safe_segment(item_id):
        return None
    if not _item_clip_re(source, item_id).match(name or ""):
        return None
    path = wav_dir(conn) / name
    return path if path.is_file() else None


def manual_clip_path(conn: sqlite3.Connection, name: str) -> Path | None:
    """Resolve one ``manual-<id>-<ts>.wav`` name (from a beacon.manual.transmitted
    audit row's ``clip`` detail) to a file on disk, or None. Rejects anything
    not matching that exact shape, so a path segment can't escape the wav dir."""
    if not _MANUAL_CLIP.match(name or ""):
        return None
    path = wav_dir(conn) / name
    return path if path.is_file() else None
