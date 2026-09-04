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

# Loopback by default: the dashboard has no authentication, and every
# state-changing action (enable transmission, send an ad-hoc message, edit an
# adapter that runs `exec`) is a plain unauthenticated POST. Binding all
# interfaces put that on the LAN for anyone who could reach the port. An
# operator who genuinely wants remote access sets UI_HOST explicitly and is
# then also responsible for putting auth in front of it.
UI_HOST = os.environ.get("UI_HOST", "127.0.0.1")
UI_PORT = int(os.environ.get("UI_PORT", "8080"))

# Host-header allow-list (DNS-rebinding guard) and cross-origin POST guard.
# Comma-separated; the defaults cover the loopback names the app is reachable
# under out of the box plus the test client's synthetic host. Add real names
# here when UI_HOST is widened.
_DEFAULT_ALLOWED_HOSTS = "127.0.0.1,localhost,testserver,[::1]"
UI_ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("UI_ALLOWED_HOSTS", _DEFAULT_ALLOWED_HOSTS).split(",")
    if h.strip()
]

# None means "use adapters.storage.DEFAULT_DB_PATH" — resolved in db.py,
# not here, so this module doesn't need to import adapters.storage just
# to read one constant.
UI_DB_PATH = os.environ.get("UI_DB_PATH") or None
