import sqlite3

from adapters.storage import get_setting
from fastapi import APIRouter, Depends, Request

from .. import queries
from ..db import get_db
from ..templating import templates
from .beacon import _status_context

router = APIRouter()


@router.get("/")
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    refresh_seconds = int(get_setting("UI_DASHBOARD_REFRESH_SECONDS", "5", conn=conn))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "counts": queries.dashboard_counts(conn),
            "recent_items": queries.recent_items(conn, limit=10),
            "recent_audit_events": queries.recent_audit_events(conn, limit=10),
            "refresh_seconds": refresh_seconds,
            **_status_context(conn),
        },
    )
