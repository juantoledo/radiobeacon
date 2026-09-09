"""Redirects adapters.storage.DEFAULT_DB_PATH to a throwaway location for
the whole test session, before any test module is collected.

actions.__main__'s module-level ACTIONS_MQ_* constants resolve via
get_setting() at import time, which falls back to opening its own
connection against DEFAULT_DB_PATH whenever a caller doesn't pass
conn/db_path explicitly (as an import-time module-level read can't).
Without this redirect, merely importing actions.__main__ (as
test_main.py/test_discover.py do) would open a connection against the
real repo's storage/radiobeacon.db."""
import tempfile
from pathlib import Path

import adapters.storage as storage_module

storage_module.DEFAULT_DB_PATH = Path(tempfile.mkdtemp(prefix="radiobeacon-test-db-")) / (
    "radiobeacon.db"
)
storage_module.get_connection(storage_module.DEFAULT_DB_PATH).close()
