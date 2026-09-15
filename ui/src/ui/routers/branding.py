"""GET /brand/logo (public) + POST /config/branding, /config/branding/remove
(admin) — the operator-uploaded instance logo. See ui.branding for the
validate/normalize/store logic this just wires up to HTTP.

Two separate APIRouters, not one: the logo has to render on login.html
before anyone is authenticated (and for a plain 'user' role in the
sidebar, which isn't admin-gated either), so its GET can carry no auth
dependency at all — the same "no dependency" shape ui.routers.auth's
router already uses for /login. The upload/remove actions stay
admin-gated, same as every other /config/* write in this app.
"""
import sqlite3
from urllib.parse import urlencode

from adapters.storage import get_brand_asset
from fastapi import APIRouter, Depends, File, Request, UploadFile
from starlette.responses import RedirectResponse, Response

from .. import branding
from ..current_user import require_role
from ..db import get_db
from ..templating import templates

router = APIRouter()
admin_router = APIRouter(dependencies=[Depends(require_role("admin"))])


@router.get("/brand/logo")
def brand_logo(conn: sqlite3.Connection = Depends(get_db)):
    response = branding.logo_response(conn)
    if response is None:
        return Response(status_code=404)
    return response


@admin_router.get("/config/branding")
def branding_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    asset = get_brand_asset(conn, "logo")
    return templates.TemplateResponse(request, "config_branding.html", {"asset": asset})


@admin_router.post("/config/branding")
async def branding_upload_action(
    logo: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    raw_bytes = await logo.read()
    try:
        branding.save_logo(conn, raw_bytes, actor="ui.branding")
    except branding.LogoUploadError as exc:
        return RedirectResponse(
            url=f"/config/branding?{urlencode({'error': str(exc)})}", status_code=303
        )
    return RedirectResponse(url="/config/branding?msg=logo+updated", status_code=303)


@admin_router.post("/config/branding/remove")
def branding_remove_action(conn: sqlite3.Connection = Depends(get_db)):
    branding.remove_logo(conn, actor="ui.branding")
    return RedirectResponse(url="/config/branding?msg=logo+removed", status_code=303)
