from adapters.custom_adapter import CustomAdapter

PASSING_CODE = """
def fetch(config):
    return [{"id": "1", "title": "Item One"}, {"id": "2", "title": "Item Two"}]
"""

RAISING_CODE = """
def fetch(config):
    raise RuntimeError("upstream boom")
"""

MALFORMED_ITEM_CODE = """
def fetch(config):
    return [{"title": "no id here"}, {"id": "ok"}]
"""

USES_PREIMPORTED_MODULES_CODE = """
import json

def fetch(config):
    payload = json.dumps({"x": 1})
    now = utc_now()
    return [{"id": "1", "contents": payload, "source_date_time": now}]
"""


LEGACY_POLICY_KEY_CODE = """
def fetch(config):
    return [{"id": "1", "dispatch_policy": "urgent"}]
"""


def test_fetch_runs_snippet_and_wraps_items():
    reading = CustomAdapter("fake", {"code": PASSING_CODE}).fetch()

    assert reading.ok
    assert [item.id for item in reading.data] == ["1", "2"]
    assert reading.data[0].title == "Item One"


def test_fetch_maps_transmit_policy():
    code = 'def fetch(config):\n    return [{"id": "1", "transmit_policy": "urgent"}]\n'
    reading = CustomAdapter("fake", {"code": code}).fetch()

    assert reading.data[0].transmit_policy == "urgent"


def test_fetch_honors_legacy_dispatch_policy_item_key():
    reading = CustomAdapter("fake", {"code": LEGACY_POLICY_KEY_CODE}).fetch()

    assert reading.data[0].transmit_policy == "urgent"


def test_fetch_returns_not_ok_when_snippet_raises():
    reading = CustomAdapter("fake", {"code": RAISING_CODE}).fetch()

    assert not reading.ok
    assert "boom" in reading.error


def test_fetch_skips_item_missing_id():
    reading = CustomAdapter("fake", {"code": MALFORMED_ITEM_CODE}).fetch()

    assert reading.ok
    assert [item.id for item in reading.data] == ["ok"]


def test_fetch_snippet_can_use_preimported_modules():
    reading = CustomAdapter("fake", {"code": USES_PREIMPORTED_MODULES_CODE}).fetch()

    assert reading.ok
    assert reading.data[0].contents == '{"x": 1}'
    assert reading.data[0].source_date_time is not None


def test_fetch_requires_fetch_function_defined():
    reading = CustomAdapter("fake", {"code": "x = 1\n"}).fetch()

    assert not reading.ok
    assert "fetch" in reading.error
