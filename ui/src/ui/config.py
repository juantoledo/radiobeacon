"""UI_HOST/UI_PORT/UI_DB_PATH — true bootstrap env vars: needed before the
UI can even reach its own database, so unlike every other setting, these
can never be DB-backed. Every other UI_* setting (page size, dev tools,
refresh intervals, default consumer name) plus DISPLAY_TIMEZONE lives in
the same DB+env+hardcoded-default catalog as every other package's
settings — see config_catalog.py's "UI"/"Display" groups and
adapters.storage.get_setting. Call sites read those live via
get_setting(..., conn=conn) at request time instead of importing a fixed
module attribute from here."""
import os

# All interfaces by default so the dashboard is reachable from the LAN (and
# through a container port mapping) out of the box. Every page (other than
# the dashboard itself) and every state-changing action now requires a
# logged-in admin (see ui.current_user) — but that's still just an
# application-level gate; put it on a trusted network, or set
# UI_HOST=127.0.0.1 with a reverse proxy in front, if you want defense in
# depth against a compromised/weak account too.
UI_HOST = os.environ.get("UI_HOST", "0.0.0.0")
UI_PORT = int(os.environ.get("UI_PORT", "8080"))

# Host-header allow-list (DNS-rebinding guard) and cross-origin POST guard.
# Comma-separated; "*" (the default) disables the check, matching the
# all-interfaces UI_HOST default. Narrow this to the exact names/IPs the app
# is served under if you want the guard back.
_DEFAULT_ALLOWED_HOSTS = "*"
UI_ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("UI_ALLOWED_HOSTS", _DEFAULT_ALLOWED_HOSTS).split(",")
    if h.strip()
]

# None means "use adapters.storage.DEFAULT_DB_PATH" — resolved in db.py,
# not here, so this module doesn't need to import adapters.storage just
# to read one constant.
UI_DB_PATH = os.environ.get("UI_DB_PATH") or None
