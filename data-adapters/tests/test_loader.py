from adapters.__main__ import load_enabled_adapters
from adapters.api_adapter import ApiAdapter
from adapters.custom_adapter import CustomAdapter
from adapters.storage import get_connection, set_adapter_instance


def test_load_enabled_adapters_builds_api_and_custom_types(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(conn, "fake-api", "api", {"url": "https://example.test"})
    set_adapter_instance(
        conn, "fake-custom", "custom", {"code": "def fetch(config):\n    return []\n"}
    )

    loaded = {source: (adapter, interval) for source, adapter, interval in load_enabled_adapters(conn)}

    assert isinstance(loaded["fake-api"][0], ApiAdapter)
    assert isinstance(loaded["fake-custom"][0], CustomAdapter)


def test_load_enabled_adapters_skips_disabled_rows(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(conn, "off", "api", {"url": "https://example.test"}, enabled=False)

    loaded = {source for source, _, _ in load_enabled_adapters(conn)}

    assert "off" not in loaded


def test_load_enabled_adapters_interval_falls_back_to_default(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.delenv("ADAPTERS_DEFAULT_INTERVAL_SECONDS", raising=False)
    set_adapter_instance(conn, "no-interval", "api", {"url": "https://example.test"})

    intervals = {source: interval for source, _, interval in load_enabled_adapters(conn)}

    assert intervals["no-interval"] == 10


def test_load_enabled_adapters_uses_row_interval_when_set(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(
        conn, "custom-interval", "api", {"url": "https://example.test"}, interval_seconds=42
    )

    intervals = {source: interval for source, _, interval in load_enabled_adapters(conn)}

    assert intervals["custom-interval"] == 42


def test_load_enabled_adapters_skips_unknown_adapter_type(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    conn.execute(
        "INSERT INTO adapter_instances (source, adapter_type, enabled, config) "
        "VALUES ('weird', 'not-a-real-type', 1, '{}')"
    )
    conn.commit()

    loaded = {source for source, _, _ in load_enabled_adapters(conn)}

    assert "weird" not in loaded
