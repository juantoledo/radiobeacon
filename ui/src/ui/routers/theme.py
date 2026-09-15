"""One endpoint behind the base.html theme switcher — the only place a
visitor's theme choice is ever persisted (see ui.theme.ThemeMiddleware,
which otherwise re-resolves the theme from the cookie on every request
without writing one)."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from ..ajax import ajax_error, ajax_ok, is_ajax
from ..current_user import get_current_user
from ..theme import SUPPORTED_THEMES, THEME_COOKIE_NAME

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.post("/theme")
def set_theme(request: Request, theme: str = Form(...), next: str = Form("/")):
    if theme not in SUPPORTED_THEMES:
        if is_ajax(request):
            return ajax_error("unsupported theme")
        raise HTTPException(status_code=400, detail="unsupported theme")
    if not next.startswith("/"):
        next = "/"
    # AJAX gets no-op JSON (the client already flipped data-theme instantly
    # in theme-switch.js — this call is only here to persist the cookie);
    # the no-JS fallback still needs its real 303 redirect.
    response = ajax_ok("") if is_ajax(request) else RedirectResponse(url=next, status_code=303)
    response.set_cookie(
        THEME_COOKIE_NAME,
        theme,
        httponly=False,
        samesite="strict",
        path="/",
        max_age=60 * 60 * 24 * 365,
    )
    return response
