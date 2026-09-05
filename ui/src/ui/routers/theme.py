"""One endpoint behind the base.html theme switcher — the only place a
visitor's theme choice is ever persisted (see ui.theme.ThemeMiddleware,
which otherwise re-resolves the theme from the cookie on every request
without writing one)."""
from fastapi import APIRouter, Depends, Form, HTTPException
from starlette.responses import RedirectResponse

from ..current_user import get_current_user
from ..theme import SUPPORTED_THEMES, THEME_COOKIE_NAME

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.post("/theme")
def set_theme(theme: str = Form(...), next: str = Form("/")):
    if theme not in SUPPORTED_THEMES:
        raise HTTPException(status_code=400, detail="unsupported theme")
    if not next.startswith("/"):
        next = "/"
    response = RedirectResponse(url=next, status_code=303)
    response.set_cookie(
        THEME_COOKIE_NAME,
        theme,
        httponly=False,
        samesite="strict",
        path="/",
        max_age=60 * 60 * 24 * 365,
    )
    return response
