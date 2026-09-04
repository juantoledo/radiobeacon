"""Composition root: builds the FastAPI app. `adapters` and `dispatcher`
resolve from the editable installs in .venv (`-e ../data-adapters` /
`-e ../dispatcher` in requirements.txt) — no sys.path juggling."""
from pathlib import Path

from adapters.storage import register_audit_event_hook
from dispatcher.mq_publisher import publish_cloud_event
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import UI_ALLOWED_HOSTS
from .security import (
    CrossOriginGuardMiddleware,
    CsrfCookieMiddleware,
    verify_csrf,
)

from .routers import (
    adapters,
    audit,
    beacon,
    config,
    dashboard,
    dev,
    items,
    manual_tx,
    policies,
    quick_settings,
)

# Publishes select audit events (item.policy_overridden, item.rearmed,
# etc. — see dispatcher/mq_publisher.py) to MQTT as CloudEvents, same as
# dispatcher/override_item.py and dispatcher/policies.py already do — the
# UI's override/rearm/policy actions go through the exact same
# dispatcher.override / adapters.transmit_policy functions those CLIs use.
# No-ops unless DISPATCHER_MQ_HOST is set.
register_audit_event_hook(publish_cloud_event)

app = FastAPI(title="radiobeacon-ui", dependencies=[Depends(verify_csrf)])
# The app has no auth; see ui.security. Added inner-to-outer: the Host
# allow-list (DNS-rebinding guard) runs first, then the cross-origin POST
# guard, then the CSRF-cookie issuer.
app.add_middleware(CsrfCookieMiddleware)
app.add_middleware(CrossOriginGuardMiddleware, allowed_hosts=UI_ALLOWED_HOSTS)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=UI_ALLOWED_HOSTS)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)

app.include_router(dashboard.router)
app.include_router(quick_settings.router)
app.include_router(manual_tx.router)
app.include_router(beacon.router)
app.include_router(items.router)
app.include_router(adapters.router)
app.include_router(policies.router)
app.include_router(config.router)
app.include_router(audit.router)
# Always mounted — dev.router itself 404s every route when
# UI_DEV_TOOLS_ENABLED is false (checked per-request, not at mount time,
# so a test can flip the flag with monkeypatch without recreating `app`).
app.include_router(dev.router)
