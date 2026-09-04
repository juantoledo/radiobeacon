"""One endpoint behind the base.html language switcher — the only place a
visitor's language choice is ever persisted (see ui.i18n.LocaleMiddleware,
which otherwise re-resolves the locale from Accept-Language on every
request without writing a cookie)."""
from fastapi import APIRouter, Form, HTTPException
from starlette.responses import RedirectResponse

from ..i18n import LOCALE_COOKIE_NAME, SUPPORTED_LOCALES

router = APIRouter()


@router.post("/locale")
def set_locale(locale: str = Form(...), next: str = Form("/")):
    if locale not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=400, detail="unsupported locale")
    if not next.startswith("/"):
        next = "/"
    response = RedirectResponse(url=next, status_code=303)
    response.set_cookie(
        LOCALE_COOKIE_NAME,
        locale,
        httponly=False,
        samesite="strict",
        path="/",
        max_age=60 * 60 * 24 * 365,
    )
    return response
