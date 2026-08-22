import sqlite3

from adapters.storage import delete_setting, get_setting, set_setting
from fastapi import APIRouter, Depends, Request
from starlette.responses import RedirectResponse

from ..beacon import BEACON_FIELDS, is_beacon_configured
from ..db import get_db
from ..templating import templates

router = APIRouter()


@router.get("/beacon")
def beacon_setup_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    values = {
        f.key: get_setting(f.key, "", conn=conn, env_fallback=False) or "" for f in BEACON_FIELDS
    }
    return templates.TemplateResponse(
        request,
        "beacon_form.html",
        {"fields": BEACON_FIELDS, "values": values, "configured": is_beacon_configured(conn), "error": None},
    )


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
            },
            status_code=400,
        )

    for f in BEACON_FIELDS:
        set_setting(conn, f.key, values[f.key], actor="ui.beacon")

    return RedirectResponse(url="/beacon?msg=beacon+identity+saved", status_code=303)
