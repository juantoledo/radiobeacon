import sqlite3
from datetime import datetime, timezone

from adapters.beacon_defaults import (
    BEACON_QUEUE_MAX_SIZE_DEFAULT,
    BEACON_WINDOW_FRAME_SECONDS_DEFAULT,
    BEACON_WINDOW_GUARD_SECONDS_DEFAULT,
    BEACON_WINDOW_TOTAL_SECONDS_DEFAULT,
    BEACON_WINDOW_VOICE_SECONDS_DEFAULT,
)
from adapters.storage import get_setting, list_beacon_status, set_setting
from fastapi import APIRouter, Depends
from starlette.responses import RedirectResponse

from ..db import get_db

router = APIRouter()

# How stale process_heartbeat_at (written every TDMA-loop tick, default
# BEACON_TICK_SECONDS=1) can be before the dashboard's beacon section
# shows "not running" — beacon/ and ui/ are separate OS processes, so
# this is the only signal the UI has that the process isn't just idle
# but has actually stalled or isn't running at all.
_HEARTBEAT_STALE_AFTER_SECONDS = 10.0

_SLOT_ORDER = ("voice", "guard", "frame", "idle")


def _format_duration(seconds: float) -> str:
    """47.7 -> "48s", 125.0 -> "2m 5s" — coarse (whole-second) display
    granularity, matching how frequently the UI actually re-polls."""
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs}s"


def _queue_bar(depth: int, max_size: int) -> dict:
    pct = 0.0 if max_size <= 0 else round(min(100.0, max(0.0, depth / max_size * 100)), 2)
    fill_class = "fill-danger" if pct >= 85 else "fill-warn" if pct >= 60 else ""
    return {"pct": pct, "fill_class": fill_class}


def _timeline_context(conn: sqlite3.Connection, status: dict) -> dict:
    """Presentation-only math for the dashboard's beacon TDMA cycle
    timeline: segment widths from the configured window, plus a "now"
    marker position from beacon's own already-computed SlotState
    (persisted to beacon_status by _write_heartbeat). Degrades
    gracefully — never raises — on a fresh install (no beacon_status
    yet) or a misconfigured window (total_seconds<=0, e.g. mid-edit in
    /config)."""
    try:
        total = int(get_setting("BEACON_WINDOW_TOTAL_SECONDS", BEACON_WINDOW_TOTAL_SECONDS_DEFAULT, conn=conn))
        voice = int(get_setting("BEACON_WINDOW_VOICE_SECONDS", BEACON_WINDOW_VOICE_SECONDS_DEFAULT, conn=conn))
        guard = int(get_setting("BEACON_WINDOW_GUARD_SECONDS", BEACON_WINDOW_GUARD_SECONDS_DEFAULT, conn=conn))
        frame = int(get_setting("BEACON_WINDOW_FRAME_SECONDS", BEACON_WINDOW_FRAME_SECONDS_DEFAULT, conn=conn))
    except ValueError:
        total = 0

    valid = total > 0 and voice + guard + frame <= total
    if not valid:
        return {"timeline_valid": False, "segments": [], "marker_pct": None}

    idle = total - voice - guard - frame
    seconds_by_slot = {"voice": voice, "guard": guard, "frame": frame, "idle": idle}
    segments = [
        {"slot": slot, "seconds": seconds_by_slot[slot], "pct": round(seconds_by_slot[slot] / total * 100, 2)}
        for slot in _SLOT_ORDER
        if seconds_by_slot[slot] > 0
    ]

    marker_pct = None
    elapsed_raw = status.get("current_cycle_elapsed_seconds")
    if elapsed_raw is not None:
        try:
            marker_pct = round(min(100.0, max(0.0, float(elapsed_raw) / total * 100)), 2)
        except ValueError:
            marker_pct = None

    return {"timeline_valid": True, "segments": segments, "marker_pct": marker_pct}


def _status_context(conn: sqlite3.Connection) -> dict:
    """Beacon transmission status — merged into the dashboard's own
    template context (see routers/dashboard.py); no refresh_seconds key
    here, the dashboard drives its own single refresh interval for the
    whole page."""
    status = list_beacon_status(conn)
    heartbeat_at = status.get("process_heartbeat_at")
    running = False
    if heartbeat_at:
        try:
            heartbeat_dt = datetime.fromisoformat(heartbeat_at)
            now = datetime.now(timezone.utc)
            running = (now - heartbeat_dt).total_seconds() < _HEARTBEAT_STALE_AFTER_SECONDS
        except ValueError:
            running = False
    enabled = get_setting("BEACON_ENABLED", "false", conn=conn).lower() == "true"

    remaining_raw = status.get("current_slot_remaining_seconds")
    remaining_display = None
    if running and remaining_raw is not None:
        try:
            remaining_display = _format_duration(float(remaining_raw))
        except ValueError:
            remaining_display = None

    try:
        queue_max_size = int(get_setting("BEACON_QUEUE_MAX_SIZE", BEACON_QUEUE_MAX_SIZE_DEFAULT, conn=conn))
    except ValueError:
        queue_max_size = int(BEACON_QUEUE_MAX_SIZE_DEFAULT)

    return {
        "status": status,
        "running": running,
        "beacon_enabled": enabled,
        "current_slot": status.get("current_slot"),
        "remaining_display": remaining_display,
        "voice_queue": _queue_bar(int(status.get("voice_queue_depth", "0")), queue_max_size),
        "frame_queue": _queue_bar(int(status.get("frame_queue_depth", "0")), queue_max_size),
        "queue_max_size": queue_max_size,
        **_timeline_context(conn, status),
    }


@router.post("/beacon/enable")
def beacon_enable_action(conn: sqlite3.Connection = Depends(get_db)):
    set_setting(conn, "BEACON_ENABLED", "true", actor="ui.beacon")
    return RedirectResponse(url="/?msg=beacon+enabled", status_code=303)


@router.post("/beacon/disable")
def beacon_disable_action(conn: sqlite3.Connection = Depends(get_db)):
    set_setting(conn, "BEACON_ENABLED", "false", actor="ui.beacon")
    return RedirectResponse(url="/?msg=beacon+disabled", status_code=303)
