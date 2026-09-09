from adapters.aiprompt_adapter import AiPromptAdapter
from adapters.__main__ import load_enabled_adapters
from adapters.api_adapter import ApiAdapter
from adapters.custom_adapter import CustomAdapter
from adapters.policy import set_policy
from adapters.storage import get_connection, set_adapter_instance


def test_load_enabled_adapters_builds_api_and_custom_types(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_policy(conn, "aip", fetch_kind="cron", fetch_cron="0 6 * * *")
    set_adapter_instance(conn, "fake-api", "api", {"url": "https://example.test"})
    set_adapter_instance(
        conn, "fake-custom", "custom", {"code": "def fetch(config):\n    return []\n"}
    )
    set_adapter_instance(
        conn, "fake-ai", "aiprompt", {"prompt": "Give a fact."}, policy="aip"
    )

    loaded = {source: (adapter, policy) for source, adapter, policy in load_enabled_adapters(conn)}

    assert isinstance(loaded["fake-api"][0], ApiAdapter)
    assert isinstance(loaded["fake-custom"][0], CustomAdapter)
    assert isinstance(loaded["fake-ai"][0], AiPromptAdapter)


def test_load_enabled_adapters_skips_disabled_rows(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(conn, "off", "api", {"url": "https://example.test"}, enabled=False)

    loaded = {source for source, _, _ in load_enabled_adapters(conn)}

    assert "off" not in loaded


def test_load_enabled_adapters_resolves_policy_by_name(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_policy(conn, "slow", fetch_kind="interval", fetch_interval_seconds=42)
    set_adapter_instance(conn, "s1", "api", {"url": "https://example.test"}, policy="slow")

    policies = {source: policy for source, _, policy in load_enabled_adapters(conn)}

    assert policies["s1"].name == "slow"
    assert policies["s1"].fetch.interval_seconds == 42


def test_load_enabled_adapters_unset_policy_falls_back_to_default(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(conn, "no-policy", "api", {"url": "https://example.test"})

    policies = {source: policy for source, _, policy in load_enabled_adapters(conn)}

    assert policies["no-policy"].name == "default"
    assert policies["no-policy"].fetch.interval_seconds == 10


def test_load_enabled_adapters_skips_unknown_adapter_type(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    conn.execute(
        "INSERT INTO adapter_instances (source, adapter_type, enabled, config) "
        "VALUES ('weird', 'not-a-real-type', 1, '{}')"
    )
    conn.commit()

    loaded = {source for source, _, _ in load_enabled_adapters(conn)}

    assert "weird" not in loaded
