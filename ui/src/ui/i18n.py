"""English/Spanish UI translation: locale resolution + a flat-JSON string
lookup. No build step, matching the rest of this app (see
templating._static_url's mtime-based cache-busting) — translations are
plain JSON dicts loaded once at import time, not a compiled gettext catalog.
Each locale is a directory, `translations/en/*.json` / `translations/es/*.json`,
one file per template/feature (e.g. `dashboard.json`, `adapter_form.json`,
`settings_beacon.json`) rather than one giant file — every scope's keys are
prefixed with its own file's basename by convention (e.g. `dashboard.title`
lives in `dashboard.json`), so two files never need to touch the same key
and can be edited independently.

Resolution order: `locale` cookie -> Accept-Language header -> UI_DEFAULT_LOCALE
setting -> "en". The cookie/header part is decided in LocaleMiddleware
(modeled on ui.security.CsrfCookieMiddleware) since it needs no DB access;
the DB-backed default-setting fallback is resolved lazily, from
templating.py's `t()`/`locale()` Jinja globals, exactly like
templating._beacon_configured_global — middleware runs before any route
dependency (including get_db), so it can't reuse request.state.db_conn the
way a Jinja global (rendered after the route body ran) can, and doing its
own always-fresh connection here would make a test's `conn` fixture
(in-memory, isolated per test) invisible to it. Only an explicit switch
(POST /locale, see routers/locale.py) ever writes the cookie — the
middleware never mints one on its own, so a visitor who hasn't chosen a
language keeps getting whatever their browser sends on every visit."""
import json
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

SUPPORTED_LOCALES = ("en", "es")
DEFAULT_LOCALE = "en"
LOCALE_COOKIE_NAME = "locale"

_TRANSLATIONS_DIR = Path(__file__).resolve().parent / "translations"


def _load_translations() -> dict[str, dict[str, str]]:
    catalog: dict[str, dict[str, str]] = {}
    for locale in SUPPORTED_LOCALES:
        locale_dir = _TRANSLATIONS_DIR / locale
        merged: dict[str, str] = {}
        if locale_dir.is_dir():
            for path in sorted(locale_dir.glob("*.json")):
                merged.update(json.loads(path.read_text(encoding="utf-8")))
        catalog[locale] = merged
    return catalog


_TRANSLATIONS = _load_translations()


def translate(key: str, locale: str, *, default: str | None = None, **kwargs) -> str:
    """Looks up `key` in `locale`'s dict, falling back to English, then to
    `default` (when the caller has one, e.g. a SettingSpec's own English
    text), then to the raw key — never raises on a missing translation."""
    text = _TRANSLATIONS.get(locale, {}).get(key)
    if text is None:
        text = _TRANSLATIONS.get(DEFAULT_LOCALE, {}).get(key)
    if text is None:
        text = default if default is not None else key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def _parse_accept_language(header: str) -> str | None:
    """Picks the highest-q supported locale (en/es) from an Accept-Language
    header's primary subtags, e.g. "es-CL,es;q=0.9,en;q=0.5" -> "es"."""
    best_locale: str | None = None
    best_q = -1.0
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        tag, _, params = part.partition(";")
        primary = tag.strip().split("-")[0].lower()
        if primary not in SUPPORTED_LOCALES:
            continue
        q = 1.0
        for param in params.split(";"):
            param = param.strip()
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 1.0
        if q > best_q:
            best_q = q
            best_locale = primary
    return best_locale


def resolve_explicit_locale(request: Request) -> str | None:
    """The cookie/Accept-Language half of resolution — no DB access, so it's
    safe to run in middleware before any route dependency exists. Returns
    None when neither source names a supported locale, leaving the
    DB-backed UI_DEFAULT_LOCALE fallback to templating.py's Jinja globals."""
    cookie = request.cookies.get(LOCALE_COOKIE_NAME)
    if cookie in SUPPORTED_LOCALES:
        return cookie

    accept_language = request.headers.get("accept-language")
    if accept_language:
        parsed = _parse_accept_language(accept_language)
        if parsed is not None:
            return parsed

    return None


class LocaleMiddleware(BaseHTTPMiddleware):
    """Stashes the cookie/Accept-Language-resolved locale (or None) on
    `request.state.locale` for templating.py's `t()`/`locale()` Jinja
    globals, which fall back to the DB-backed UI_DEFAULT_LOCALE setting
    when this is None. Never sets a cookie itself — see module docstring."""

    async def dispatch(self, request: Request, call_next):
        request.state.locale = resolve_explicit_locale(request)
        return await call_next(request)
