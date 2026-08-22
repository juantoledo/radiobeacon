"""UI_*-prefixed env vars this module reads — see root .env.example.
Naming/prefix convention matches every other package (ADAPTERS_*,
DISPATCHER_*, ACTIONS_*)."""
import os

UI_HOST = os.environ.get("UI_HOST", "0.0.0.0")
UI_PORT = int(os.environ.get("UI_PORT", "8000"))

# None means "use adapters.storage.DEFAULT_DB_PATH" — resolved in db.py,
# not here, so this module doesn't need to import adapters.storage just
# to read one constant.
UI_DB_PATH = os.environ.get("UI_DB_PATH") or None

UI_PAGE_SIZE = int(os.environ.get("UI_PAGE_SIZE", "50"))

# Reuses DISPATCHER_CONSUMER_NAME (same logical consumer dispatcher.README
# describes) rather than requiring a second var naming the same thing.
UI_DEFAULT_CONSUMER_NAME = os.environ.get(
    "UI_DEFAULT_CONSUMER_NAME", os.environ.get("DISPATCHER_CONSUMER_NAME", "log")
)

# How often (seconds) the dashboard's browser-side auto-refresh re-polls
# for new items/audit events — same "poll interval" pattern as
# ADAPTERS_DEFAULT_INTERVAL_SECONDS/DISPATCHER_INTERVAL_SECONDS, just
# client-side: there's no cross-process DB change notification to hook
# into (data-adapters/dispatcher write from separate processes/
# connections), so polling is the mechanism, same as every other consumer
# of storage/radiobeacon.db in this repo. 0 disables auto-refresh.
UI_DASHBOARD_REFRESH_SECONDS = int(os.environ.get("UI_DASHBOARD_REFRESH_SECONDS", "5"))

# Enables the Developers section (/dev) — direct add/edit/delete on raw
# items (bypassing the "items are immutable after insert" contract
# adapters.storage.store_reading otherwise enforces), a dispatcher-state
# debug reset, and a read-only ad hoc SQL runner. A meaningfully larger
# attack surface than the rest of this auth-less, localhost-by-default
# app — on by default since it's a deliberately requested tool, but an
# operator who wants the browse/override/policy pages without it can
# disable this specific surface without disabling the whole UI.
UI_DEV_TOOLS_ENABLED = os.environ.get("UI_DEV_TOOLS_ENABLED", "true").lower() not in (
    "false",
    "0",
    "",
)
