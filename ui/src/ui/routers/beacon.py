import sqlite3
from datetime import datetime, timezone

from adapters.beacon_defaults import BEACON_QUEUE_MAX_SIZE_DEFAULT, BEACON_TYPE_DEFAULT
from adapters.storage import get_setting, list_beacon_status, set_setting
from fastapi import APIRouter, Depends
from starlette.responses import RedirectResponse

from ..db import get_db

router = APIRouter()

# How stale process_heartbeat_at (written every transmit-loop tick, default
# BEACON_TICK_SECONDS=2) can be before the dashboard's beacon section shows
# "not running" — beacon/ and ui/ are separate OS processes, so this is the
# only signal the UI has that the process isn't just idle but has actually
# stalled or isn't running at all.
_HEARTBEAT_STALE_AFTER_SECONDS = 10.0


def _queue_bar(depth: int, max_size: int) -> dict:
    pct = 0.0 if max_size <= 0 else round(min(100.0, max(0.0, depth / max_size * 100)), 2)
    fill_class = "fill-danger" if pct >= 85 else "fill-warn" if pct >= 60 else ""
    return {"pct": pct, "fill_class": fill_class}


def _status_context(conn: sqlite3.Connection) -> dict:
    """Beacon transmission status — merged into the dashboard's own template
    context (see routers/dashboard.py); no refresh_seconds key here, the
    dashboard drives its own single refresh interval for the whole page."""
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
    beacon_type = status.get("beacon_type") or get_setting(
        "BEACON_TYPE", BEACON_TYPE_DEFAULT, conn=conn
    )

    try:
        queue_max_size = int(get_setting("BEACON_QUEUE_MAX_SIZE", BEACON_QUEUE_MAX_SIZE_DEFAULT, conn=conn))
    except ValueError:
        queue_max_size = int(BEACON_QUEUE_MAX_SIZE_DEFAULT)

    depth_key = "frame_queue_depth" if beacon_type == "frame" else "voice_queue_depth"
    try:
        queue_depth = int(status.get(depth_key, "0"))
    except ValueError:
        queue_depth = 0

    last_transmit_at = status.get(
        "last_frame_transmit_at" if beacon_type == "frame" else "last_voice_transmit_at"
    )

    return {
        "status": status,
        "running": running,
        "beacon_enabled": enabled,
        "beacon_type": beacon_type,
        "queue_depth": queue_depth,
        "queue_bar": _queue_bar(queue_depth, queue_max_size),
        "queue_max_size": queue_max_size,
        "last_transmit_at": last_transmit_at,
    }


@router.post("/beacon/enable")
def beacon_enable_action(conn: sqlite3.Connection = Depends(get_db)):
    set_setting(conn, "BEACON_ENABLED", "true", actor="ui.beacon")
    return RedirectResponse(url="/?msg=beacon+enabled", status_code=303)


@router.post("/beacon/disable")
def beacon_disable_action(conn: sqlite3.Connection = Depends(get_db)):
    set_setting(conn, "BEACON_ENABLED", "false", actor="ui.beacon")
    return RedirectResponse(url="/?msg=beacon+disabled", status_code=303)
