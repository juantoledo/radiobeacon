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
from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse

from ..ajax import ajax_error, ajax_ok, is_ajax
from ..current_user import require_role
from ..db import get_db
from ..templating import templates
from .dashboard import _dashboard_context

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


def _render_live(request: Request, conn: sqlite3.Connection) -> str:
    """The dashboard's live region, re-rendered after a toggle — same
    context builder dashboard.py's own GET / and its X-Auto-Refresh
    fragment path use, so an AJAX toggle response can be patched into the
    page with the exact same RB.patchCells cell-diffing an auto-refresh
    poll would apply."""
    return templates.get_template("_dashboard_live.html").render(
        request=request, **_dashboard_context(conn)
    )


@router.post("/dashboard/toggle")
def dashboard_toggle(
    request: Request,
    key: str = Form(...),
    value: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    if key in _BOOL_TOGGLES:
        normalized = "true" if value.lower() == "true" else "false"
        label = _BOOL_TOGGLES[key]
    elif key in _CHOICE_TOGGLES:
        if value not in _CHOICE_TOGGLES[key]:
            if is_ajax(request):
                return ajax_error("invalid value")
            return RedirectResponse(url="/?error=invalid+value", status_code=303)
        normalized = value
        label = key
    else:
        if is_ajax(request):
            return ajax_error("that setting is not toggleable here")
        return RedirectResponse(url="/?error=that+setting+is+not+toggleable+here", status_code=303)

    set_setting(conn, key, normalized, actor="ui.dashboard")
    if is_ajax(request):
        return ajax_ok(f"{label} set to {normalized}", fragment=_render_live(request, conn))
    return RedirectResponse(url=f"/?msg={label}+set+to+{normalized}", status_code=303)
