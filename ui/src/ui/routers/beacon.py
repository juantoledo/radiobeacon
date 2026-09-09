import sqlite3
from datetime import datetime, timezone

from adapters.beacon_defaults import BEACON_QUEUE_MAX_SIZE_DEFAULT, BEACON_TYPE_DEFAULT
from adapters.storage import get_setting, list_beacon_status, set_setting
from fastapi import APIRouter, Depends
from starlette.responses import RedirectResponse

from ..current_user import require_role
from ..db import get_db

router = APIRouter(dependencies=[Depends(require_role("admin"))])

# How stale process_heartbeat_at (written every transmit-loop tick, default
# BEACON_TICK_SECONDS=2) can be before the dashboard's beacon section shows
# "not running" — beacon/ and ui/ are separate OS processes, so this is the
# only signal the UI has that the process isn't just idle but has actually
# stalled or isn't running at all.
_HEARTBEAT_STALE_AFTER_SECONDS = 10.0

# tx_monitor (beacon.tx_monitor) writes tx_monitor_heartbeat_at every ~2s
# while it can read the SvxLink log. Older than this -> the monitor isn't
# running or can't reach the log, so we show no "ON AIR" state at all.
_TX_MONITOR_STALE_AFTER_SECONDS = 15.0

# Guard against a stale tx_keyed="1" pinning the indicator on if the
# monitor's own watchdog somehow didn't clear it (it should, after 300s).
_TX_KEYED_STALE_AFTER_SECONDS = 330.0


def _is_fresh(iso_value: str | None, max_age_seconds: float) -> bool:
    if not iso_value:
        return False
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - parsed).total_seconds() < max_age_seconds


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
    # The configured mode is the operator's intent and what every control
    # on the dashboard reflects — flipping BEACON_TYPE must show instantly,
    # not wait for the beacon process to rewrite its telemetry (or never,
    # if it isn't running). `beacon_type_live` keeps the last value the
    # process actually reported, so the UI can flag drift between the two.
    beacon_type = get_setting("BEACON_TYPE", BEACON_TYPE_DEFAULT, conn=conn)
    beacon_type_live = status.get("beacon_type")

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

    # Live transmit state from beacon.tx_monitor's SvxLink log tail. Both
    # flags are False whenever the monitor isn't reporting, so the
    # dashboard indicator stays hidden unless we genuinely know the rig is
    # keyed right now.
    tx_monitor_active = _is_fresh(
        status.get("tx_monitor_heartbeat_at"), _TX_MONITOR_STALE_AFTER_SECONDS
    )
    on_air = (
        tx_monitor_active
        and status.get("tx_keyed") == "1"
        and _is_fresh(status.get("tx_keyed_at"), _TX_KEYED_STALE_AFTER_SECONDS)
    )

    return {
        "status": status,
        "running": running,
        "beacon_enabled": enabled,
        "beacon_type": beacon_type,
        "beacon_type_live": beacon_type_live,
        "queue_depth": queue_depth,
        "queue_bar": _queue_bar(queue_depth, queue_max_size),
        "queue_max_size": queue_max_size,
        "last_transmit_at": last_transmit_at,
        "tx_monitor_active": tx_monitor_active,
        "on_air": on_air,
    }


@router.post("/beacon/enable")
def beacon_enable_action(conn: sqlite3.Connection = Depends(get_db)):
    set_setting(conn, "BEACON_ENABLED", "true", actor="ui.beacon")
    return RedirectResponse(url="/?msg=beacon+enabled", status_code=303)


@router.post("/beacon/disable")
def beacon_disable_action(conn: sqlite3.Connection = Depends(get_db)):
    set_setting(conn, "BEACON_ENABLED", "false", actor="ui.beacon")
    return RedirectResponse(url="/?msg=beacon+disabled", status_code=303)
