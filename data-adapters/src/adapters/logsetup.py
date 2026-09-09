"""One logging setup shared by every package's entrypoint.

`adapters` is the package every other package already depends on (see
`adapters.storage`), so the shared helper lives here rather than being
copy-pasted into each `__main__`.

- `configure_logging(service)` — called once at process start by
  data-adapters / dispatcher / actions / beacon / ui. Installs a single
  root handler with the standard format and sets the level from
  `LOG_LEVEL` (a `/config`-editable setting: DB row -> env var -> "INFO",
  via `adapters.storage.get_setting`).
- `refresh_level(conn=None)` — cheap re-read of `LOG_LEVEL`, applied to
  the root logger if it changed. Long-running services call this once per
  loop tick so a `/config` change takes effect without a restart.

Message convention (applied across all five packages): the logger name
carries the service/module, so messages have no `"beacon: "`-style prefix;
identifying context is trailing `key=value` tokens (`source=`, `item_id=`,
`event_id=`, `topic=`, `provider=`, `count=`); caught exceptions are
logged with `exc_info=True` rather than `": %s" % exc`.
"""
import logging
import sys

from adapters.storage import get_setting

STANDARD_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

LOG_LEVEL_DEFAULT = "INFO"
LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR")

# Explicit rather than logging.getLevelNamesMapping() (3.11+) — this
# package supports 3.10 (see pyproject.toml requires-python).
_NAME_TO_LEVEL = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# Third-party loggers pinned to WARNING unless the resolved level is DEBUG
# (then they follow the root level) — their INFO/DEBUG chatter otherwise
# drowns out this app's own lines.
_NOISY_LIBRARIES = ("httpx", "httpcore", "urllib3", "paho", "paho.mqtt", "asyncio", "multipart")

_logger = logging.getLogger("adapters.logsetup")
_warned_unknown: set[str] = set()
_our_handler: logging.Handler | None = None


def resolve_level(conn=None, db_path=None) -> int:
    """The numeric level `LOG_LEVEL` currently resolves to. Unknown values
    fall back to INFO (warned once)."""
    raw = (
        get_setting("LOG_LEVEL", LOG_LEVEL_DEFAULT, conn=conn, db_path=db_path)
        or LOG_LEVEL_DEFAULT
    ).strip().upper()
    if raw not in _NAME_TO_LEVEL:
        if raw not in _warned_unknown:
            _warned_unknown.add(raw)
            _logger.warning("unknown LOG_LEVEL %r, using %s", raw, LOG_LEVEL_DEFAULT)
        return _NAME_TO_LEVEL[LOG_LEVEL_DEFAULT]
    return _NAME_TO_LEVEL[raw]


def _apply_library_levels(root_level: int) -> None:
    lib_level = logging.NOTSET if root_level <= logging.DEBUG else logging.WARNING
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(lib_level)


def configure_logging(service: str, *, conn=None, db_path=None) -> None:
    """Install the one root handler and set the level from `LOG_LEVEL`.
    Idempotent — safe to call more than once (replaces the handler it
    installed before, and leaves any other handler, e.g. a test harness's,
    alone)."""
    global _our_handler
    level = resolve_level(conn, db_path)
    root = logging.getLogger()
    if _our_handler is not None and _our_handler in root.handlers:
        root.removeHandler(_our_handler)
    _our_handler = logging.StreamHandler(sys.stderr)
    _our_handler.setFormatter(logging.Formatter(STANDARD_FORMAT))
    root.addHandler(_our_handler)
    root.setLevel(level)
    _apply_library_levels(level)
    _logger.debug("logging configured service=%s level=%s", service, logging.getLevelName(level))


def refresh_level(conn=None, db_path=None) -> None:
    """Re-read `LOG_LEVEL` and, if it changed, apply it to the root logger.
    Logs the transition at the new level so it is always visible."""
    new = resolve_level(conn, db_path)
    root = logging.getLogger()
    if root.level == new:
        return
    old_name = logging.getLevelName(root.level)
    root.setLevel(new)
    _apply_library_levels(new)
    _logger.log(new, "log level %s -> %s", old_name, logging.getLevelName(new))
