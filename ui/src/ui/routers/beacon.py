import sqlite3
from datetime import datetime, timezone

from adapters.storage import delete_setting, get_setting, list_beacon_status, set_setting
from fastapi import APIRouter, Depends, Request
from starlette.responses import RedirectResponse

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
    return {"status": status, "running": running, "beacon_enabled": enabled}


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
