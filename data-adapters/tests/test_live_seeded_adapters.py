"""Integration smoke tests (excluded by default, see pytest.ini) proving
the seeded csn (api) / senapred (custom) adapter_instances rows still
reproduce live, working fetches through the new generic ApiAdapter/
CustomAdapter — a parity check against the pre-refactor hand-written
CsnAdapter/SenapredAdapter modules these replaced."""
import json

import pytest

from adapters.__main__ import build_adapter
from adapters.storage import get_adapter_instance, get_connection


@pytest.mark.integration
def test_fetch_against_live_csn_api(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    row = get_adapter_instance(conn, "csn")
    adapter = build_adapter(row["adapter_type"], row["source"], json.loads(row["config"]))

    reading = adapter.fetch()

    assert reading.source == "csn"
    if not reading.ok:
        pytest.fail(f"live CSN fetch failed: {reading.error}")


@pytest.mark.integration
def test_fetch_against_live_senapred_backend(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    row = get_adapter_instance(conn, "senapred")
    adapter = build_adapter(row["adapter_type"], row["source"], json.loads(row["config"]))

    reading = adapter.fetch()

    assert reading.source == "senapred"
    if not reading.ok:
        pytest.fail(f"live SENAPRED fetch failed: {reading.error}")
