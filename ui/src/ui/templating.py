"""Shared Jinja2Templates instance + filters, imported by both app.py and
every router — kept separate from app.py so routers don't have to import
the composition root (which would be circular, since app.py imports the
routers)."""
from datetime import datetime
from pathlib import Path

from adapters.storage import DEFAULT_DB_PATH, get_connection, get_setting
from adapters.timeutil import to_display_tz, to_utc
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup

from . import config
from .beacon import is_beacon_configured
from .icons import render_icon

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _icon_global(name: str, size: int = 16) -> Markup:
    return Markup(render_icon(name, size))


templates.env.globals["icon"] = _icon_global


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


def _policy_badge_class(name: str | None) -> str:
    """Maps a dispatch_policy name to a badge color — purely presentational,
    not a schema concept: "urgent"/"informational" are just the two names
    dispatcher.policy seeds by default (see dispatcher/README.md), any
    other name (operator-defined via the policy form) falls back to a
    neutral badge rather than guessing at its severity."""
    if not name:
        return "badge badge-neutral"
    if name == "urgent":
        return "badge badge-urgent"
    if name == "informational":
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
        "item.rearmed",
        "item.policy_drifted",
        "item.dev_edited",
        "item.dispatch_state_reset",
    ):
        return "badge badge-warn"
    if event_type in ("item.dispatched", "item.discovered", "item.stored", "item.created"):
        return "badge badge-success"
    return "badge badge-neutral"


templates.env.filters["policy_badge"] = _policy_badge_class
templates.env.filters["event_badge"] = _event_badge_class
