"""Composition root: sys.path bootstrap for cross-module imports, then
builds the FastAPI app. Same precedent as dispatcher/override_item.py /
dispatcher/policies.py / dispatcher/src/dispatcher/__main__.py, each of
which does this sys.path.insert at the top of its own entry-point file
rather than relying on PYTHONPATH alone. Routers are only ever imported
from this module (below, after the insert), so they can freely import
adapters.* / dispatcher.* without repeating the bootstrap themselves."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "data-adapters" / "src"))
sys.path.insert(0, str(REPO_ROOT / "dispatcher" / "src"))

from adapters.storage import register_audit_event_hook  # noqa: E402
from dispatcher.mq_publisher import publish_cloud_event  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from .routers import (  # noqa: E402
    adapters,
    audit,
    beacon,
    config,
    dashboard,
    dev,
    items,
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

app = FastAPI(title="radiobeacon-ui")
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)

app.include_router(dashboard.router)
app.include_router(quick_settings.router)
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
