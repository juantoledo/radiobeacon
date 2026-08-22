"""Shared Jinja2Templates instance + filters, imported by both app.py and
every router — kept separate from app.py so routers don't have to import
the composition root (which would be circular, since app.py imports the
routers)."""
from datetime import datetime
from pathlib import Path

from adapters.timeutil import to_display_tz, to_utc
from fastapi.templating import Jinja2Templates

from . import config

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# The module itself, not its current UI_DEV_TOOLS_ENABLED value — Jinja
# reads config.UI_DEV_TOOLS_ENABLED fresh on every render (module
# attribute access, not a value snapshotted at import time), matching
# dev.router's own per-request check in _require_dev_tools_enabled. Lets
# base.html hide the Developers nav link entirely when the section is
# disabled, rather than showing a link that 404s — and lets a test flip
# the flag with monkeypatch and see both the router and the nav react.
templates.env.globals["config"] = config


def _display_dt(value: str | None) -> str:
    """Converts a stored timestamp to DISPLAY_TIMEZONE for display only —
    never fed back into a query. Handles both Python's offset-suffixed
    ISO 8601 (fetched_at, source_date_time, audit_log-hook-written values)
    and SQLite's own datetime('now') format with no offset marker
    (items.captured_at, audit_log.recorded_at) — see root README's "Dates
    and times: always UTC" note on that documented format difference."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = to_utc(dt, assume_tz="UTC")
    return to_display_tz(dt).strftime("%Y-%m-%d %H:%M:%S %Z")


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
