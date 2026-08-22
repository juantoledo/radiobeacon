"""Redirects adapters.storage.DEFAULT_DB_PATH to a throwaway location for
the whole test session, before any test module is collected.

Several adapters (csn, senapred) resolve their module-level config via
get_setting() at import time, and get_setting() falls back to opening its
own connection against DEFAULT_DB_PATH whenever a caller doesn't pass
conn/db_path explicitly (as an import-time module-level read can't).
Without this redirect, merely importing/collecting those modules would
open a connection against the real repo's storage/radiobeacon.db — this
runs at conftest import time (before any fixture, before any test module
in this directory is collected) so it's in place before that first import
happens."""
import tempfile
from pathlib import Path

import adapters.storage as storage_module

storage_module.DEFAULT_DB_PATH = Path(tempfile.mkdtemp(prefix="radiobeacon-test-db-")) / (
    "radiobeacon.db"
)
# Actually create the file (fully migrated schema) — some code paths open
# it in SQLite's read-only URI mode, which (unlike a normal connect) can't
# create a missing file.
storage_module.get_connection(storage_module.DEFAULT_DB_PATH).close()
