"""Shared helpers for routes returning an ajax_ok/ajax_error `fragment=`
— see ajax.py for the response contract and static/ajax-forms.js for the
client half that patches [data-cell] regions from it."""
import sqlite3

from starlette.requests import Request

from .templating import templates


def render_fragment(request: Request, template_name: str, context: dict) -> str:
    return templates.get_template(template_name).render(request=request, **context)


def render_dashboard_live(request: Request, conn: sqlite3.Connection) -> str:
    """The dashboard's live region — same fragment quick_settings.py's
    /dashboard/toggle already uses, moved here so beacon.py's enable/
    disable and manual_tx.py's transmit action can render the identical
    fragment without importing across routers."""
    from .routers.dashboard import _dashboard_context

    return render_fragment(request, "_dashboard_live.html", _dashboard_context(conn, request))
