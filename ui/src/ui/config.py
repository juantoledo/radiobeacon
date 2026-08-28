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

UI_HOST = os.environ.get("UI_HOST", "0.0.0.0")
UI_PORT = int(os.environ.get("UI_PORT", "8080"))

# None means "use adapters.storage.DEFAULT_DB_PATH" — resolved in db.py,
# not here, so this module doesn't need to import adapters.storage just
# to read one constant.
UI_DB_PATH = os.environ.get("UI_DB_PATH") or None
