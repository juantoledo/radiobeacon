import sqlite3
from datetime import datetime, timezone

from adapters.storage import delete_setting, get_setting, list_beacon_status, set_setting
from fastapi import APIRouter, Depends, Request
from starlette.responses import RedirectResponse

from .. import config
from ..beacon import BEACON_FIELDS, is_beacon_configured
from ..db import get_db
from ..templating import templates

router = APIRouter()

# How stale process_heartbeat_at (written every TDMA-loop tick, default
# BEACON_TICK_SECONDS=1) can be before the status page shows "not running"
# — beacon/ and ui/ are separate OS processes, so this is the only signal
# the UI has that the process isn't just idle but has actually stalled or
# isn't running at all.
_HEARTBEAT_STALE_AFTER_SECONDS = 10.0

# Same literal defaults as beacon/src/beacon/__main__.py's own
# get_setting(...) calls for these keys — duplicated here (not imported;
# no direct import exists between ui/ and beacon/, only via
# data-adapters, same as every other cross-package setting default
# already duplicated in config_catalog.py) purely for presentation math
# (the timeline's segment widths). beacon/schedule.py stays the single
# owner of the actual scheduling logic.
_DEFAULT_WINDOW_TOTAL_SECONDS = 90
_DEFAULT_WINDOW_VOICE_SECONDS = 60
_DEFAULT_WINDOW_GUARD_SECONDS = 0
_DEFAULT_WINDOW_FRAME_SECONDS = 30
_DEFAULT_QUEUE_MAX_SIZE = 20

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
    """Presentation-only math for the /beacon cycle timeline: segment
    widths from the configured window, plus a "now" marker position from
    beacon's own already-computed SlotState (persisted to beacon_status
    by _write_heartbeat). Degrades gracefully — never raises — on a
    fresh install (no beacon_status yet) or a misconfigured window
    (total_seconds<=0, e.g. mid-edit in /config)."""
    try:
        total = int(get_setting("BEACON_WINDOW_TOTAL_SECONDS", str(_DEFAULT_WINDOW_TOTAL_SECONDS), conn=conn))
        voice = int(get_setting("BEACON_WINDOW_VOICE_SECONDS", str(_DEFAULT_WINDOW_VOICE_SECONDS), conn=conn))
        guard = int(get_setting("BEACON_WINDOW_GUARD_SECONDS", str(_DEFAULT_WINDOW_GUARD_SECONDS), conn=conn))
        frame = int(get_setting("BEACON_WINDOW_FRAME_SECONDS", str(_DEFAULT_WINDOW_FRAME_SECONDS), conn=conn))
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
        queue_max_size = int(get_setting("BEACON_QUEUE_MAX_SIZE", str(_DEFAULT_QUEUE_MAX_SIZE), conn=conn))
    except ValueError:
        queue_max_size = _DEFAULT_QUEUE_MAX_SIZE

    return {
        "status": status,
        "running": running,
        "beacon_enabled": enabled,
        "current_slot": status.get("current_slot"),
        "remaining_display": remaining_display,
        "voice_queue": _queue_bar(int(status.get("voice_queue_depth", "0")), queue_max_size),
        "frame_queue": _queue_bar(int(status.get("frame_queue_depth", "0")), queue_max_size),
        "queue_max_size": queue_max_size,
        "refresh_seconds": config.UI_BEACON_REFRESH_SECONDS,
        **_timeline_context(conn, status),
    }


@router.get("/beacon")
def beacon_setup_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    values = {
        f.key: get_setting(f.key, "", conn=conn, env_fallback=False) or "" for f in BEACON_FIELDS
    }
    return templates.TemplateResponse(
        request,
        "beacon_form.html",
        {
            "fields": BEACON_FIELDS,
            "values": values,
            "configured": is_beacon_configured(conn),
            "error": None,
            **_status_context(conn),
        },
    )


@router.post("/beacon/enable")
def beacon_enable_action(conn: sqlite3.Connection = Depends(get_db)):
    set_setting(conn, "BEACON_ENABLED", "true", actor="ui.beacon")
    return RedirectResponse(url="/beacon?msg=beacon+enabled", status_code=303)


@router.post("/beacon/disable")
def beacon_disable_action(conn: sqlite3.Connection = Depends(get_db)):
    set_setting(conn, "BEACON_ENABLED", "false", actor="ui.beacon")
    return RedirectResponse(url="/beacon?msg=beacon+disabled", status_code=303)


@router.post("/beacon")
async def beacon_setup_save_action(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    values = {f.key: (form.get(f.key) or "").strip() for f in BEACON_FIELDS}

    missing = [f.label for f in BEACON_FIELDS if not values[f.key]]
    if missing:
        return templates.TemplateResponse(
            request,
            "beacon_form.html",
            {
                "fields": BEACON_FIELDS,
                "values": values,
                "configured": is_beacon_configured(conn),
                "error": f"Required: {', '.join(missing)}",
                **_status_context(conn),
            },
            status_code=400,
        )

    for f in BEACON_FIELDS:
        set_setting(conn, f.key, values[f.key], actor="ui.beacon")

    return RedirectResponse(url="/beacon?msg=beacon+identity+saved", status_code=303)
