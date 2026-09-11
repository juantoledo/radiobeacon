"""Composition root: builds the FastAPI app. `adapters` and `dispatcher`
resolve from the editable installs in .venv (`-e ../data-adapters` /
`-e ../dispatcher` in requirements.txt) — no sys.path juggling."""
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from adapters.auth import bootstrap_admin_if_missing
from adapters.logsetup import refresh_level
from adapters.storage import DEFAULT_DB_PATH, get_connection, register_audit_event_hook
from dispatcher.mq_publisher import publish_cloud_event
from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import RedirectResponse

from . import config
from .config import UI_ALLOWED_HOSTS
from .current_user import NotAuthenticated
from .i18n import LocaleMiddleware
from .security import (
    CrossOriginGuardMiddleware,
    CsrfCookieMiddleware,
    verify_csrf,
)
from .setup import SetupRequired
from .theme import ThemeMiddleware

from .routers import (
    about,
    adapters,
    audit,
    auth,
    beacon,
    config as config_router,
    config_transfer,
    dashboard,
    dev,
    items,
    locale,
    manual_tx,
    policies,
    quick_settings,
    rf_conf,
    setup as setup_router,
    theme,
    users,
)

logger = logging.getLogger(__name__)

# Publishes select audit events (item.policy_overridden, item.rearmed,
# etc. — see dispatcher/mq_publisher.py) to MQTT as CloudEvents, same as
# dispatcher/override_item.py and dispatcher/policies.py already do — the
# UI's override/rearm/policy actions go through the exact same
# dispatcher.override / adapters.policy functions those CLIs use.
# No-ops unless DISPATCHER_MQ_HOST is set.
register_audit_event_hook(publish_cloud_event)


_LOG_LEVEL_POLL_SECONDS = 30


def _watch_log_level(stop_event: threading.Event, db_path) -> None:
    """Re-read the LOG_LEVEL setting on a timer so a /config change applies
    to the running server without a restart — the daemon-thread equivalent
    of the refresh_level() call each backend service makes once per loop
    tick."""
    while not stop_event.wait(_LOG_LEVEL_POLL_SECONDS):
        try:
            refresh_level(db_path=db_path)
        except Exception:
            logger.warning("log level refresh failed", exc_info=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db_path = config.UI_DB_PATH or DEFAULT_DB_PATH
    log_level_stop = threading.Event()
    log_level_thread = threading.Thread(
        target=_watch_log_level, args=(log_level_stop, db_path),
        name="ui-log-level", daemon=True,
    )
    log_level_thread.start()
    # Ensures an admin account always exists — the very first thing a
    # fresh install (or a database an operator emptied out) needs before
    # /login is usable at all. bootstrap_admin_if_missing no-ops (returns
    # None) whenever an enabled admin already exists, so this never
    # re-prints/re-generates on a normal restart — the admin row's own
    # presence *is* the idempotency check, same idiom as
    # _ensure_adapter_instances_seeded's "only seed an empty table".
    conn = get_connection(config.UI_DB_PATH or DEFAULT_DB_PATH)
    try:
        created = bootstrap_admin_if_missing(conn)
    finally:
        conn.close()
    if created:
        username, password = created
        logger.warning(
            "\n===== INITIAL ADMIN PASSWORD =====\n"
            "username: %s\npassword: %s\n"
            "Log in and change this password immediately (or reset it with "
            "data-adapters/manage_users.sh set-password %s) — it is only "
            "ever printed once.\n===================================",
            username,
            password,
            username,
        )
    try:
        yield
    finally:
        log_level_stop.set()
        from . import tx_stream

        await tx_stream.hub.aclose()


app = FastAPI(title="radiobeacon-ui", dependencies=[Depends(verify_csrf)], lifespan=_lifespan)


@app.exception_handler(NotAuthenticated)
def _redirect_to_login(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url=f"/login?next={quote(exc.next_path)}", status_code=303)


@app.exception_handler(SetupRequired)
def _redirect_to_setup(request: Request, exc: SetupRequired):
    return RedirectResponse(url="/setup", status_code=303)


# Login (ui.current_user's session-cookie dependency, gating every router
# below except auth.router's own /login) sits alongside this middleware
# stack conceptually but isn't one of them — see ui.current_user's
# docstring for why it has to be a FastAPI dependency instead. Added
# inner-to-outer: the Host allow-list (DNS-rebinding guard) runs first,
# then the cross-origin POST guard, then the CSRF-cookie issuer.
app.add_middleware(CsrfCookieMiddleware)
app.add_middleware(LocaleMiddleware)
app.add_middleware(ThemeMiddleware)
app.add_middleware(CrossOriginGuardMiddleware, allowed_hosts=UI_ALLOWED_HOSTS)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=UI_ALLOWED_HOSTS)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)

app.include_router(auth.router)
app.include_router(setup_router.router)
app.include_router(dashboard.router)
app.include_router(about.router)
app.include_router(locale.router)
app.include_router(theme.router)
app.include_router(quick_settings.router)
app.include_router(manual_tx.router)
app.include_router(beacon.router)
app.include_router(items.router)
app.include_router(adapters.router)
app.include_router(policies.router)
# Before config_router: rf_conf owns GET /config/beacon-{svxlink,direwolf}
# and config_transfer owns /config/import-export/*, both of which
# config_router's GET /config/{slug} catch-all would otherwise handle.
app.include_router(rf_conf.router)
app.include_router(config_transfer.router)
app.include_router(config_router.router)
app.include_router(audit.router)
app.include_router(users.router)
# Always mounted — dev.router itself 404s every route when
# UI_DEV_TOOLS_ENABLED is false (checked per-request, not at mount time,
# so a test can flip the flag with monkeypatch without recreating `app`).
app.include_router(dev.router)
