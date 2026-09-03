"""Locating the WAV clips the beacon renders for voice transmissions so the
dashboard can play them back in the browser.

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
from pathlib import Path

from adapters.storage import get_setting

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Remainder after the ``{source}-{item_id}-`` prefix for a voice clip: just
# the unix timestamp. A frame clip's remainder is ``{chunk_index}-{ts}``,
# which this rejects.
_VOICE_REMAINDER = re.compile(r"\d+\.wav\Z")


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
