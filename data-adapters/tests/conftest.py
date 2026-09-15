"""Redirects adapters.storage.DEFAULT_DB_PATH to a throwaway location for
the whole test session, before any test module is collected.

get_connection() (called below, and by anything that opens a connection
without an explicit db_path) creates and seeds the full schema — including
the adapter_instances table's csn/senapred rows — against whatever
DEFAULT_DB_PATH currently points at. Without this redirect, merely
importing adapters.storage would risk touching the real repo's
storage/radiobeacon.db — this runs at conftest import time (before any
fixture, before any test module in this directory is collected) so it's in
place before that first open happens."""
import tempfile
from pathlib import Path

import adapters.storage as storage_module
import pytest

storage_module.DEFAULT_DB_PATH = Path(tempfile.mkdtemp(prefix="radiobeacon-test-db-")) / (
    "radiobeacon.db"
)
# Actually create the file (fully migrated schema) — some code paths open
# it in SQLite's read-only URI mode, which (unlike a normal connect) can't
# create a missing file.
storage_module.get_connection(storage_module.DEFAULT_DB_PATH).close()


@pytest.fixture(autouse=True)
def _reset_schema_bootstrap_cache():
    """get_connection()/the various _ensure_*_table helpers now bootstrap
    a real database file's schema at most once per process (see
    adapters.storage._ensure_bootstrapped) — this clears that cache before
    every test so inode reuse across different tests' temp dirs within
    this one pytest process can't produce a false cache hit."""
    from adapters.storage import reset_schema_bootstrap_cache

    reset_schema_bootstrap_cache()
    yield
