import tempfile
from pathlib import Path

import adapters.storage as storage_module
import pytest

# get_setting() (used throughout beacon's modules) falls back to opening
# its own connection against DEFAULT_DB_PATH whenever a caller doesn't
# pass conn/db_path explicitly. Without this redirect, importing
# beacon.__main__ (whose module-level BEACON_MQ_* constants call
# get_setting with no conn) would open a connection against the real
# repo's storage/radiobeacon.db — see the equivalent conftest.py in the
# other three packages' test suites.
storage_module.DEFAULT_DB_PATH = Path(tempfile.mkdtemp(prefix="radiobeacon-test-db-")) / (
    "radiobeacon.db"
)
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
