"""Visitor-chosen light/dark theme override, modeled on i18n.py's locale
resolution but simpler: no translation catalog, no Accept-* header
equivalent — just a cookie. "system" means no override (the CSS
`prefers-color-scheme` media query decides); "light"/"dark" pin the palette
regardless of OS preference.

Resolution order: `theme` cookie -> UI_DEFAULT_THEME setting -> "system".
The cookie part is decided in ThemeMiddleware (modeled on
i18n.LocaleMiddleware) since it needs no DB access; the DB-backed
default-setting fallback is resolved lazily, from templating.py's `theme()`
Jinja global, exactly like i18n's `locale()`. Only an explicit switch
(POST /theme, see routers/theme.py) ever writes the cookie."""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

SUPPORTED_THEMES = ("system", "light", "dark")
DEFAULT_THEME = "system"
THEME_COOKIE_NAME = "theme"


def resolve_explicit_theme(request: Request) -> str | None:
    """The cookie half of resolution — no DB access, so it's safe to run in
    middleware before any route dependency exists. Returns None when the
    cookie is absent or names an unsupported value, leaving the DB-backed
    UI_DEFAULT_THEME fallback to templating.py's `theme()` Jinja global."""
    cookie = request.cookies.get(THEME_COOKIE_NAME)
    if cookie in SUPPORTED_THEMES:
        return cookie
    return None


class ThemeMiddleware(BaseHTTPMiddleware):
    """Stashes the cookie-resolved theme (or None) on `request.state.theme`
    for templating.py's `theme()` Jinja global, which falls back to the
    DB-backed UI_DEFAULT_THEME setting when this is None. Never sets a
    cookie itself — see module docstring."""

    async def dispatch(self, request: Request, call_next):
        request.state.theme = resolve_explicit_theme(request)
        return await call_next(request)
