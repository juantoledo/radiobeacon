"""Cron-expression evaluation for the AI Prompt adapter's regeneration
schedule (see adapters.aiprompt_adapter).

`croniter` is imported lazily inside each function so importing the package
never requires it — same pattern as adapters.llm's provider SDKs. Only the
`aiprompt` adapter path and the UI's adapter-form validation pull it in.

Expressions are evaluated in DISPLAY_TIMEZONE (adapters.timeutil — DB row ->
env -> "America/Santiago"): an operator writing "0 6 * * *" means 06:00
local, not UTC. Every value returned is converted back to a UTC-aware
datetime, per the repo's "always UTC internally" rule.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .storage import get_setting
from .timeutil import DEFAULT_DISPLAY_TIMEZONE, DISPLAY_TIMEZONE_ENV_VAR

logger = logging.getLogger(__name__)


def is_valid_cron(expr: str) -> bool:
    """Whether `expr` is a cron expression croniter can evaluate."""
    if not expr or not expr.strip():
        return False
    try:
        from croniter import croniter
    except ImportError:  # pragma: no cover - croniter is a declared dependency
        logger.error("croniter is not installed; cannot validate cron expression %r", expr)
        return False
    return croniter.is_valid(expr.strip())


def _display_tz(conn) -> ZoneInfo:
    return ZoneInfo(
        get_setting(DISPLAY_TIMEZONE_ENV_VAR, DEFAULT_DISPLAY_TIMEZONE, conn=conn)
    )


def latest_fire_at_or_before(expr: str, at: datetime, *, conn) -> datetime | None:
    """The most recent cron occurrence at or before `at`, as a UTC-aware
    datetime. Returns None if `expr` is invalid.

    Used as the AI Prompt adapter's per-cycle dedup anchor: two polls in the
    same cron window resolve to the same occurrence, hence the same item id,
    so the LLM is called at most once per occurrence.
    """
    if not is_valid_cron(expr):
        logger.error("invalid cron expression %r", expr)
        return None
    from croniter import croniter

    # croniter.get_prev() is exclusive of an exact boundary, so nudge the
    # base forward a second to make "at or before" genuinely inclusive when
    # `at` lands exactly on an occurrence.
    base = at.astimezone(_display_tz(conn)) + timedelta(seconds=1)
    return croniter(expr.strip(), base).get_prev(datetime).astimezone(timezone.utc)


def next_fire_after(expr: str, after: datetime, *, conn) -> datetime | None:
    """The next cron occurrence strictly after `after`, as a UTC-aware
    datetime (for a UI "next run" hint). None if `expr` is invalid."""
    if not is_valid_cron(expr):
        return None
    from croniter import croniter

    base = after.astimezone(_display_tz(conn))
    return croniter(expr.strip(), base).get_next(datetime).astimezone(timezone.utc)
