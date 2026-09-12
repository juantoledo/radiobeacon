"""Shared Jinja2Templates instance + filters, imported by both app.py and
every router — kept separate from app.py so routers don't have to import
the composition root (which would be circular, since app.py imports the
routers)."""
from datetime import datetime, timezone
from pathlib import Path

from adapters import __version__ as ADAPTERS_VERSION
from adapters.categories import get_category, get_subtype
from adapters.storage import DEFAULT_DB_PATH, get_connection, get_setting
from adapters.timeutil import to_display_tz, to_utc
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup

from . import config
from .beacon import is_beacon_configured
from .config_catalog import NAV_CATEGORY_ORDER, category_slug
from .i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, translate
from .icons import render_icon
from .setup import is_setup_complete
from .theme import DEFAULT_THEME, SUPPORTED_THEMES

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _static_url(path: str) -> str:
    """`/static/<path>?v=<mtime>` — the query string changes whenever the
    file does, so a browser can't keep serving a stale style.css / JS after
    a deploy (the UI has no other cache-busting; StaticFiles sends no
    max-age). Falls back to an unversioned URL if the file is missing."""
    try:
        version = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={version}"


templates.env.globals["static_url"] = _static_url


def _icon_global(name: str, size: int = 16) -> Markup:
    return Markup(render_icon(name, size))


templates.env.globals["icon"] = _icon_global

# `{{ app_version() }}` — the whole-project version (VERSION at the repo
# root, resolved once by adapters.version at import time), shown in the
# site-wide footer.
templates.env.globals["app_version"] = lambda: ADAPTERS_VERSION


@pass_context
def _csrf_field(context) -> Markup:
    """`{{ csrf_field() }}` inside every state-changing <form> — the hidden
    input the double-submit-cookie check (ui.security.verify_csrf) compares
    against the csrf_token cookie."""
    token = getattr(context["request"].state, "csrf_token", "")
    return Markup(f'<input type="hidden" name="csrf_token" value="{token}">')


templates.env.globals["csrf_field"] = _csrf_field


@pass_context
def _locale_global(context) -> str:
    """`{{ locale() }}` — the active request's resolved language ("en"/"es").
    i18n.LocaleMiddleware already resolved the cookie/Accept-Language half
    onto request.state.locale (or left it None); when neither named a
    supported locale, falls back to the DB-backed UI_DEFAULT_LOCALE setting
    using the same per-request db_conn-reuse idiom as
    _beacon_configured_global/_dev_tools_enabled_global above — middleware
    runs before get_db, so it can't reuse request.state.db_conn itself,
    which is why this DB fallback lives here instead."""
    request = context["request"]
    explicit = getattr(request.state, "locale", None)
    if explicit in SUPPORTED_LOCALES:
        return explicit
    conn = getattr(request.state, "db_conn", None)
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection(config.UI_DB_PATH or DEFAULT_DB_PATH, check_same_thread=False)
    try:
        default_locale = get_setting("UI_DEFAULT_LOCALE", DEFAULT_LOCALE, conn=conn)
    finally:
        if owns_conn:
            conn.close()
    return default_locale if default_locale in SUPPORTED_LOCALES else DEFAULT_LOCALE


templates.env.globals["locale"] = _locale_global


@pass_context
def _theme_global(context) -> str:
    """`{{ theme() }}` — the active request's resolved theme
    ("system"/"light"/"dark"). ui.theme.ThemeMiddleware already resolved the
    cookie half onto request.state.theme (or left it None); when that's
    None, falls back to the DB-backed UI_DEFAULT_THEME setting using the
    same per-request db_conn-reuse idiom as _locale_global above."""
    request = context["request"]
    explicit = getattr(request.state, "theme", None)
    if explicit in SUPPORTED_THEMES:
        return explicit
    conn = getattr(request.state, "db_conn", None)
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection(config.UI_DB_PATH or DEFAULT_DB_PATH, check_same_thread=False)
    try:
        default_theme = get_setting("UI_DEFAULT_THEME", DEFAULT_THEME, conn=conn)
    finally:
        if owns_conn:
            conn.close()
    return default_theme if default_theme in SUPPORTED_THEMES else DEFAULT_THEME


templates.env.globals["theme"] = _theme_global


@pass_context
def _t(context, key: str, *, default: str | None = None, **kwargs) -> str:
    """`{{ t('nav.dashboard') }}` — looks up `key` in the active locale's
    translation dict (see i18n.translate for the fallback chain: locale ->
    English -> `default` -> the raw key)."""
    return translate(key, _locale_global(context), default=default, **kwargs)


templates.env.globals["t"] = _t

_DEFAULT_CATEGORY_ICON = "tag"


@pass_context
def _type_label(context, type_key: str | None) -> str:
    """`{{ type_label(item.type) }}` — the localized label for a recognized
    adapters.categories key, or the raw stored string verbatim for anything
    unrecognized (legacy data, or a custom adapter's own free-text value) —
    this never raises and never hides a value the operator actually stored."""
    category = get_category(type_key)
    if category is None:
        return type_key or ""
    return category.label_es if _locale_global(context) == "es" else category.label_en


templates.env.globals["type_label"] = _type_label


@pass_context
def _subtype_label(context, type_key: str | None, subtype_key: str | None) -> str:
    """`{{ subtype_label(item.type, item.subtype) }}` — same fallback rule as
    _type_label, one level down (a subtype is only resolved within its
    parent category, so an unrecognized/mismatched type_key falls back to
    the raw subtype string too)."""
    subtype = get_subtype(type_key, subtype_key)
    if subtype is None:
        return subtype_key or ""
    return subtype.label_es if _locale_global(context) == "es" else subtype.label_en


templates.env.globals["subtype_label"] = _subtype_label


@pass_context
def _category_label(context, type_key: str | None, subtype_key: str | None = None) -> str:
    """`{{ category_label(item.type, item.subtype) }}` — combined "Type ·
    Subtype" display for surfaces that show them on one line."""
    type_part = _type_label(context, type_key)
    subtype_part = _subtype_label(context, type_key, subtype_key) if subtype_key else ""
    if type_part and subtype_part:
        return f"{type_part} · {subtype_part}"
    return type_part or subtype_part


templates.env.globals["category_label"] = _category_label


def _category_icon(type_key: str | None, subtype_key: str | None = None) -> str:
    """`{{ icon(category_icon(item.type, item.subtype), 14) }}` — resolves
    to a subtype-specific icon override when one exists, else the parent
    category's icon, else the generic fallback icon for anything
    unrecognized (including no type at all)."""
    subtype = get_subtype(type_key, subtype_key)
    if subtype is not None and subtype.icon:
        return subtype.icon
    category = get_category(type_key)
    if category is not None:
        return category.icon
    return _DEFAULT_CATEGORY_ICON


templates.env.globals["category_icon"] = _category_icon

# Icon per /config tab — purely presentational, keyed by category name (see
# NAV_CATEGORY_ORDER) plus the one non-catalog tab, Policies.
_CONFIG_TAB_ICONS: dict[str, str] = {
    "Adapters": "layers",
    "Dispatcher": "activity",
    "MQ": "database",
    "Actions": "zap",
    "Beacon": "beacon",
    "Secrets": "lock",
    "Display": "activity",
    "UI": "config",
    "Logging": "monitor",
    "Policies": "policies",
    "Import/Export": "transfer",
}


def _config_nav_tabs_global() -> list[dict]:
    """Tabs for the /config subnav — one per settings category (in
    NAV_CATEGORY_ORDER) plus Policies and Import/Export, which live outside
    SETTINGS_CATALOG entirely (see ui.routers.policies /
    ui.routers.config_transfer) but are folded into the same Config section
    rather than separate top-level pages."""
    tabs = [
        {"name": category, "slug": category_slug(category), "icon": _CONFIG_TAB_ICONS[category]}
        for category in NAV_CATEGORY_ORDER
    ]
    tabs.append({"name": "Policies", "slug": "policies", "icon": _CONFIG_TAB_ICONS["Policies"]})
    tabs.append(
        {
            "name": "Import/Export",
            "slug": "import-export",
            "icon": _CONFIG_TAB_ICONS["Import/Export"],
        }
    )
    return tabs


templates.env.globals["config_nav_tabs"] = _config_nav_tabs_global


@pass_context
def _dev_tools_enabled_global(context) -> bool:
    """Jinja global so base.html/item_detail.html can hide dev-only UI
    without every router passing this into its own template context.
    Same per-request db_conn-reuse idiom as _beacon_configured_global
    below — reads UI_DEV_TOOLS_ENABLED fresh on every render (DB row ->
    env var -> default), matching dev.router's own per-request check in
    _require_dev_tools_enabled, so a /config edit is reflected with no
    restart."""
    request = context["request"]
    conn = getattr(request.state, "db_conn", None)
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection(config.UI_DB_PATH or DEFAULT_DB_PATH, check_same_thread=False)
    try:
        return get_setting("UI_DEV_TOOLS_ENABLED", "true", conn=conn).lower() not in (
            "false",
            "0",
            "",
        )
    finally:
        if owns_conn:
            conn.close()


templates.env.globals["dev_tools_enabled"] = _dev_tools_enabled_global


@pass_context
def _beacon_configured_global(context) -> bool:
    """Jinja global so base.html's sitewide banner/nav badge work on every
    page without every router adding it to its own context dict.

    @pass_context (not a plain zero-arg global) so this can reuse the
    current request's own db_conn — stashed onto request.state by
    ui.db.get_db() — rather than always opening a second, independent
    connection to DEFAULT_DB_PATH. That distinction matters in tests: the
    `client` fixture overrides get_db to yield an isolated in-memory
    connection, which a plain self-opened connection to DEFAULT_DB_PATH
    would never see, making the banner always render as "not configured"
    regardless of what a test just set. request.state.db_conn is only
    absent for a request that never depended on get_db (none currently
    render a template without it) — the self-opened fallback covers that
    hypothetical case, same short-lived "owns_conn" idiom
    adapters.storage.get_setting uses when called without conn=."""
    request = context["request"]
    conn = getattr(request.state, "db_conn", None)
    if conn is not None:
        return is_beacon_configured(conn)
    conn = get_connection(config.UI_DB_PATH or DEFAULT_DB_PATH, check_same_thread=False)
    try:
        return is_beacon_configured(conn)
    finally:
        conn.close()


templates.env.globals["is_beacon_configured"] = _beacon_configured_global


@pass_context
def _setup_complete_global(context) -> bool:
    """`{{ is_setup_complete() }}` — same per-request db_conn-reuse idiom as
    _beacon_configured_global above (and the same test-fixture-visibility
    reason it matters), for the /setup wizard's own sticky completion flag."""
    request = context["request"]
    conn = getattr(request.state, "db_conn", None)
    if conn is not None:
        return is_setup_complete(conn)
    conn = get_connection(config.UI_DB_PATH or DEFAULT_DB_PATH, check_same_thread=False)
    try:
        return is_setup_complete(conn)
    finally:
        conn.close()


templates.env.globals["is_setup_complete"] = _setup_complete_global


@pass_context
def _current_user_global(context):
    """`{{ current_user() }}` — the logged-in User (or None on /login,
    where get_current_user never ran), stashed onto request.state.user by
    ui.current_user.get_current_user. Templates use this for the sidebar's
    logged-in-as/logout control and to hide admin-only nav/controls from a
    'user'-role visitor."""
    return getattr(context["request"].state, "user", None)


templates.env.globals["current_user"] = _current_user_global


@pass_context
def _is_admin_global(context) -> bool:
    """`{{ is_admin() }}` — shorthand for the current_user().role == 'admin'
    check every admin-only template guard needs."""
    user = _current_user_global(context)
    return user is not None and user.role == "admin"


templates.env.globals["is_admin"] = _is_admin_global


@pass_context
def _display_dt(context, value: str | None) -> str:
    """Converts a stored timestamp to DISPLAY_TIMEZONE for display only —
    never fed back into a query. Handles both Python's offset-suffixed
    ISO 8601 (fetched_at, source_date_time, audit_log-hook-written values)
    and SQLite's own datetime('now') format with no offset marker
    (items.captured_at, audit_log.recorded_at) — see root README's "Dates
    and times: always UTC" note on that documented format difference.

    @pass_context so DISPLAY_TIMEZONE resolves live (DB row -> env var ->
    default) via the same per-request db_conn-reuse idiom as
    _dev_tools_enabled_global/_beacon_configured_global, honoring a
    UI_DB_PATH override rather than get_setting's own fallback (which
    always targets adapters.storage.DEFAULT_DB_PATH)."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = to_utc(dt, assume_tz="UTC")
    request = context["request"]
    conn = getattr(request.state, "db_conn", None)
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection(config.UI_DB_PATH or DEFAULT_DB_PATH, check_same_thread=False)
    try:
        return to_display_tz(dt, conn=conn).strftime("%Y-%m-%d %H:%M:%S %Z")
    finally:
        if owns_conn:
            conn.close()


templates.env.filters["display_dt"] = _display_dt


def _parse_utc(value: str | None) -> datetime | None:
    """Parses either timestamp shape this repo stores — Python's
    offset-suffixed ISO 8601 (fetched_at, source_date_time, beacon_status
    heartbeats) or SQLite's own offset-less datetime('now') (captured_at,
    recorded_at) — into an aware UTC datetime. See _display_dt for the same
    dual-format handling."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(value: str | None) -> str:
    """Normalizes a stored timestamp to a `Z`-suffixed UTC ISO string for a
    `<time datetime=...>` attribute — the machine-readable anchor the
    dashboard's client-side "3m ago" ticker counts from."""
    dt = _parse_utc(value)
    return "" if dt is None else dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_ago(value: str | None) -> str:
    """Compact relative time ("just now", "3m ago", "5h ago", "2d ago") —
    the no-JS fallback text inside the dashboard's <time> elements, which
    dashboard-refresh.js then keeps current in the browser."""
    dt = _parse_utc(value)
    if dt is None:
        return "—"
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


templates.env.filters["iso_utc"] = _iso_utc
templates.env.filters["time_ago"] = _time_ago


def _policy_badge_class(name: str | None) -> str:
    """Maps a Policy name to a badge color — purely presentational, not a
    schema concept: a fresh install only seeds "default" (see
    adapters.policy). "urgent" is the conventional name for an escalation
    tier an operator may add via the policy form, so it keeps a dedicated
    color; any other name falls back to a neutral badge rather than
    guessing at its severity."""
    if not name:
        return "badge badge-neutral"
    if name == "urgent":
        return "badge badge-urgent"
    if name == "default":
        return "badge badge-info"
    return "badge badge-neutral"


def _event_badge_class(event_type: str | None) -> str:
    """Maps an audit_log event_type to a badge color by its family, purely
    presentational — see adapters.storage.record_audit_event's contract
    for the full set of event types this repo writes."""
    if not event_type:
        return "badge badge-neutral"
    if event_type.endswith("_failed") or event_type == "item.deleted":
        return "badge badge-danger"
    if event_type in (
        "item.policy_overridden",
        "item.reprocessed",
        "item.policy_drifted",
        "item.dev_edited",
        "item.dispatch_state_reset",
        "item.ai_aborted",
        "beacon.tx.skipped_stale",
        "beacon.tx.superseded",
        "beacon.tx.supersede_skip",
        "beacon.watermark.skipped_no_callsign",
        "beacon.watermark.dropped_too_long",
        "beacon.manual.skipped_no_callsign",
        "beacon.manual.dropped_too_long",
    ):
        return "badge badge-warn"
    if event_type in (
        "item.dispatched",
        "item.discovered",
        "item.stored",
        "item.created",
        "beacon.watermark.transmitted",
        "beacon.manual.transmitted",
    ):
        return "badge badge-success"
    return "badge badge-neutral"


templates.env.filters["policy_badge"] = _policy_badge_class
templates.env.filters["event_badge"] = _event_badge_class
