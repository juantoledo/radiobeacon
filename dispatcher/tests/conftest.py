"""Redirects adapters.storage.DEFAULT_DB_PATH to a throwaway location for
the whole test session, before any test module is collected.

get_setting() (used by dispatcher.__main__'s module-level config and by
every DISPATCHER_MQ_*/ACTIONS_MQ_* lookup in mq_publisher.py) falls back to
opening its own connection against DEFAULT_DB_PATH whenever a caller
doesn't pass conn/db_path explicitly. Without this redirect, tests that
exercise those code paths without a per-test override would open a
connection against the real repo's storage/radiobeacon.db."""
import tempfile
from pathlib import Path

import adapters.storage as storage_module

storage_module.DEFAULT_DB_PATH = Path(tempfile.mkdtemp(prefix="radiobeacon-test-db-")) / (
    "radiobeacon.db"
)
storage_module.get_connection(storage_module.DEFAULT_DB_PATH).close()
