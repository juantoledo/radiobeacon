"""One narrow endpoint behind the dashboard's "Quick controls" panel.

Every key here is a setting the running pipeline re-reads live (no
process restart needed to take effect) — that's the whole bar for
appearing on the dashboard. The allow-list is the security boundary:
this auth-less app must not grow a second, unvalidated path to the full
settings surface, so anything not named below is refused. Writes go
through the same adapters.storage.set_setting every /config edit uses,
with a distinct `ui.dashboard` actor in audit_log.
"""
import sqlite3

from adapters.storage import set_setting
from fastapi import APIRouter, Depends, Form
from starlette.responses import RedirectResponse

from ..current_user import require_role
from ..db import get_db

router = APIRouter(dependencies=[Depends(require_role("admin"))])

# Live-applied booleans safe to flip from the dashboard. Labels here are
# only for the redirect toast; the panel's own template owns the UI copy.
_BOOL_TOGGLES: dict[str, str] = {
    "BEACON_ENABLED": "Beacon transmit",
    "BEACON_WATERMARK_ENABLED": "Periodic watermark",
    "ACTIONS_AI_ENABLED": "AI summarization",
    "UI_DEV_TOOLS_ENABLED": "Developer tools",
}

# Small fixed-choice switches (not booleans).
_CHOICE_TOGGLES: dict[str, tuple[str, ...]] = {
    "BEACON_TYPE": ("voice", "frame"),
}


@router.post("/dashboard/toggle")
def dashboard_toggle(
    key: str = Form(...),
    value: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    if key in _BOOL_TOGGLES:
        normalized = "true" if value.lower() == "true" else "false"
        label = _BOOL_TOGGLES[key]
    elif key in _CHOICE_TOGGLES:
        if value not in _CHOICE_TOGGLES[key]:
            return RedirectResponse(url="/?error=invalid+value", status_code=303)
        normalized = value
        label = key
    else:
        return RedirectResponse(url="/?error=that+setting+is+not+toggleable+here", status_code=303)

    set_setting(conn, key, normalized, actor="ui.dashboard")
    return RedirectResponse(url=f"/?msg={label}+set+to+{normalized}", status_code=303)
