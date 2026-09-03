"""Locating the WAV clips the beacon renders (per-item voice bulletins and
one-shot manual transmissions) so the dashboard can play them back in the
browser.

The beacon writes one WAV per voice unit into BEACON_TTS_WAV_DIR, named
``{source}-{item_id}-{unix_ts}.wav`` (frame/packet clips get an extra
``-{chunk_index}`` segment and are *not* offered for playback — they're
AFSK modem tones, not speech). beacon.__main__ anchors a relative
BEACON_TTS_WAV_DIR at the repo root, and ui/docker-compose.yml bind-mounts
that same top-level storage/ directory, so both processes see one shared
folder. Nothing here ever opens a caller-supplied path — every returned
Path comes from iterating the directory itself.
"""
import re
import sqlite3
from datetime import datetime, timezone
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


def items_with_voice_clips(
    conn: sqlite3.Connection, items: list[sqlite3.Row]
) -> set[tuple[str, str]]:
    """Which of ``items`` have at least one rendered voice clip — one
    directory listing, not one glob per item."""
    try:
        names = [
            entry.name
            for entry in wav_dir(conn).iterdir()
            if entry.is_file() and entry.name.endswith(".wav")
        ]
    except OSError:
        return set()
    out: set[tuple[str, str]] = set()
    for item in items:
        key = (item["source"], item["item_id"])
        if key in out:
            continue
        prefix = f"{item['source']}-{item['item_id']}-"
        if any(
            name.startswith(prefix) and _VOICE_REMAINDER.match(name[len(prefix):])
            for name in names
        ):
            out.add(key)
    return out


def recent_manual_clips(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """The most recently rendered manual-transmission clips, newest first:
    ``{"name", "manual_id", "at"}`` (``at`` = the file's mtime as an ISO
    UTC string, the closest stand-in for when it went on air)."""
    try:
        entries = [
            entry
            for entry in wav_dir(conn).iterdir()
            if entry.is_file() and _MANUAL_CLIP.match(entry.name)
        ]
    except OSError:
        return []
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    clips = []
    for entry in entries[:limit]:
        match = _MANUAL_CLIP.match(entry.name)
        clips.append(
            {
                "name": entry.name,
                "manual_id": int(match.group(1)),
                "at": datetime.fromtimestamp(
                    entry.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )
    return clips


def manual_clip_path(conn: sqlite3.Connection, name: str) -> Path | None:
    """Resolve one ``manual-<id>-<ts>.wav`` name (from recent_manual_clips)
    to a file on disk, or None. Rejects anything not matching that exact
    shape, so a path segment can't escape the wav dir."""
    if not _MANUAL_CLIP.match(name or ""):
        return None
    path = wav_dir(conn) / name
    return path if path.is_file() else None
