import sqlite3

from fastapi import APIRouter, Depends, Request

from .. import config, queries
from ..db import get_db
from ..templating import templates

router = APIRouter()


@router.get("/")
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    # Read via the config module (not a name imported at module load
    # time) so a test can monkeypatch config.UI_DASHBOARD_REFRESH_SECONDS
    # per-request.
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "counts": queries.dashboard_counts(conn),
            "recent_items": queries.recent_items(conn, limit=10),
            "recent_audit_events": queries.recent_audit_events(conn, limit=10),
            "refresh_seconds": config.UI_DASHBOARD_REFRESH_SECONDS,
        },
    )
