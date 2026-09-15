"""Single source of truth for "now" and timezone-normalization across every
package in this repo (data-adapters, dispatcher, actions) — see the root
README's "Dates and times: always UTC" rule. Any package needing the
current instant, or needing to normalize a naive/local datetime received
from an external source, should go through this module rather than
calling datetime.now() or datetime.utcnow() directly."""
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .storage import get_setting

# For presentation only (a future UI, a human-readable log line, etc.) —
# never for anything stored or compared internally, which stays UTC
# unconditionally per the "always UTC" rule. Resolved via get_setting
# (DB row -> env var -> default) at call time, not import time, so it's
# editable live via /config's "Display" group with no restart needed
# anywhere it's read.
DISPLAY_TIMEZONE_ENV_VAR = "DISPLAY_TIMEZONE"
DEFAULT_DISPLAY_TIMEZONE = "America/Santiago"


def utc_now() -> datetime:
    """The one blessed way to get "now" anywhere in this repo. Always
    timezone-aware, always UTC. Never use bare datetime.now() (silently
    depends on the running host's local timezone — a real bug, fixed in
    the senapred/csn adapters) or datetime.utcnow() (deprecated, and
    silently naive)."""
    return datetime.now(timezone.utc)


def to_utc(dt: datetime, *, assume_tz: str | None = None) -> datetime:
    """Normalizes any datetime to a UTC-aware one.

    If `dt` is already timezone-aware, converts it via
    .astimezone(timezone.utc) regardless of what offset it already
    carries — correct for a source that mixes offsets across items (e.g.
    SENAPRED's Alerta feed returns "Z", its Evento feed returns
    "-04:00"; both are handled the same way here).

    If `dt` is naive, `assume_tz` (an IANA zone name, e.g.
    "America/Santiago") is REQUIRED, stating which zone the naive value
    is understood to represent. Raises ValueError if omitted — an
    unlabeled naive datetime silently treated as UTC is exactly the bug
    class this module exists to prevent."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    if assume_tz is None:
        raise ValueError(
            "to_utc() received a naive datetime with no assume_tz — "
            "state which timezone it represents before converting"
        )
    return dt.replace(tzinfo=ZoneInfo(assume_tz)).astimezone(timezone.utc)


def resolve_display_tz(*, conn: sqlite3.Connection | None = None) -> ZoneInfo:
    """The configured DISPLAY_TIMEZONE (DB row -> env var -> "America/Santiago",
    see adapters.storage.get_setting) as a ZoneInfo. Split out of
    to_display_tz so a caller rendering many timestamps in one request (e.g.
    ui.templating._display_dt, once per timestamp on a page) can resolve it
    once instead of re-querying get_setting on every call.

    Pass conn to reuse an already-open connection instead of opening a
    new short-lived one via get_setting's own owns_conn fallback (which
    targets adapters.storage.DEFAULT_DB_PATH — wrong for a caller whose
    DB path is overridden, e.g. ui's UI_DB_PATH)."""
    tz_name = get_setting(DISPLAY_TIMEZONE_ENV_VAR, DEFAULT_DISPLAY_TIMEZONE, conn=conn)
    return ZoneInfo(tz_name)


def to_display_tz(dt: datetime, *, conn: sqlite3.Connection | None = None) -> datetime:
    """Converts a UTC (or any tz-aware) datetime to this repo's
    configured DISPLAY_TIMEZONE (DB row -> env var -> "America/Santiago",
    see adapters.storage.get_setting) — for presentation only: the UI, a
    human-readable log/notification line, anything shown to a person.
    Internal storage/processing must stay on utc_now()/to_utc() — never
    pass this function's result back into anything persisted or compared
    against other stored datetimes; see "Dates and times: always UTC" in
    README.md.

    Pass conn to reuse an already-open connection instead of opening a
    new short-lived one via get_setting's own owns_conn fallback (which
    targets adapters.storage.DEFAULT_DB_PATH — wrong for a caller whose
    DB path is overridden, e.g. ui's UI_DB_PATH).

    Requires `dt` to already be tz-aware (raises ValueError otherwise —
    an unlabeled naive datetime has no defined instant to convert; call
    to_utc() first if you're starting from a naive source value)."""
    if dt.tzinfo is None:
        raise ValueError(
            "to_display_tz() received a naive datetime — convert it "
            "with to_utc() first, there's no defined instant to display "
            "otherwise"
        )
    return dt.astimezone(resolve_display_tz(conn=conn))
