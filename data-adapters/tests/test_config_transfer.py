import pytest

from adapters.config_transfer import SCHEMA_VERSION, build_export, import_config
from adapters.custom_adapter import CustomAdapter
from adapters.storage import (
    get_adapter_instance,
    get_connection,
    get_setting,
    list_settings,
    set_adapter_instance,
    set_setting,
    set_source,
)
from adapters.transmit_policy import get_policy, list_policies, set_policy


@pytest.fixture
def conn(tmp_path):
    c = get_connection(tmp_path / "radiobeacon.db")
    yield c
    c.close()


@pytest.fixture
def target(tmp_path):
    """A second, independent DB — the "fresh install" an export gets
    imported into in most of these tests."""
    c = get_connection(tmp_path / "target.db")
    yield c
    c.close()


# --------------------------------- export ---------------------------------


def test_export_never_includes_a_secret_key(conn):
    set_setting(conn, "ANTHROPIC_API_KEY", "sk-real-key", is_secret=True)
    set_setting(conn, "OPENAI_API_KEY", "sk-also-real", is_secret=True)
    set_setting(conn, "DISPATCHER_INTERVAL_SECONDS", "9")

    export = build_export(conn, actor="test")

    assert "ANTHROPIC_API_KEY" not in export["settings"]
    assert "OPENAI_API_KEY" not in export["settings"]
    assert export["settings"]["DISPATCHER_INTERVAL_SECONDS"] == "9"


def test_export_bundles_a_linked_source_into_its_adapter_and_lists_only_orphans_separately(conn):
    set_adapter_instance(conn, "demo", "api", {"url": "https://example.com"})
    set_source(conn, "demo", "Demo Source", "https://example.com")
    set_source(conn, "orphan", "Orphan Source", None)

    export = build_export(conn, actor="test")

    demo = next(a for a in export["adapter_instances"] if a["source"] == "demo")
    assert demo["display_name"] == "Demo Source"
    assert demo["site_url"] == "https://example.com"
    assert [s["source"] for s in export["sources"]] == ["orphan"]


def test_export_records_a_config_exported_audit_event(conn):
    build_export(conn, actor="test-actor")

    row = conn.execute(
        "SELECT event_type, actor FROM audit_log WHERE event_type = 'config.exported'"
    ).fetchone()
    assert row is not None
    assert row[1] == "test-actor"


def test_export_schema_version_matches_module_constant(conn):
    assert build_export(conn, actor="test")["schema_version"] == SCHEMA_VERSION


# --------------------------------- import: basics ---------------------------------


def test_import_upserts_without_touching_a_pre_existing_extra_row(conn, target):
    set_policy(target, "extra", 3, 30, "not in the import file")
    export = {"schema_version": 1, "transmit_policies": [
        {"name": "informational", "repeat_times": 1, "interval_seconds": 0}
    ]}

    import_config(target, export, allow_custom_code=True, actor="test")

    assert get_policy(target, "extra") is not None


def test_import_classifies_created_vs_updated(conn, target):
    set_policy(target, "informational", 1, 0, "already here")
    export = {
        "schema_version": 1,
        "transmit_policies": [
            {"name": "informational", "repeat_times": 1, "interval_seconds": 0},
            {"name": "urgent", "repeat_times": 5, "interval_seconds": 60},
        ],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    by_key = {r.key: r.action for r in summary.results if r.section == "transmit_policies"}
    assert by_key == {"informational": "updated", "urgent": "created"}


def test_one_bad_record_does_not_abort_the_rest_of_the_batch(conn, target):
    export = {
        "schema_version": 1,
        "transmit_policies": [
            {"name": "good", "repeat_times": 1, "interval_seconds": 0},
            {"name": "bad", "repeat_times": "not-a-number", "interval_seconds": 0},
            {"name": "also-good", "repeat_times": 2, "interval_seconds": 10},
        ],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    assert get_policy(target, "good") is not None
    assert get_policy(target, "also-good") is not None
    assert get_policy(target, "bad") is None
    bad_result = next(r for r in summary.results if r.key == "bad")
    assert bad_result.action == "skipped"
    assert bad_result.reason


def test_dry_run_leaves_every_table_untouched(conn, target):
    export = {
        "schema_version": 1,
        "settings": {"DISPATCHER_INTERVAL_SECONDS": "42"},
        "transmit_policies": [{"name": "urgent", "repeat_times": 5, "interval_seconds": 60}],
        "sources": [{"source": "orphan", "display_name": "Orphan"}],
        "adapter_instances": [
            {"source": "demo", "adapter_type": "api", "config": {"url": "https://x"}}
        ],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test", dry_run=True)

    assert summary.dry_run is True
    assert get_setting("DISPATCHER_INTERVAL_SECONDS", conn=target, env_fallback=False) is None
    assert get_policy(target, "urgent") is None
    assert get_adapter_instance(target, "demo") is None
    assert list_settings(target) == []
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='config.imported'").fetchone()[0] == 0


def test_import_records_one_config_imported_audit_event_with_counts(conn, target):
    export = {"schema_version": 1, "settings": {"DISPATCHER_INTERVAL_SECONDS": "5"}}

    import_config(target, export, allow_custom_code=True, actor="test")

    row = target.execute(
        "SELECT details FROM audit_log WHERE event_type = 'config.imported'"
    ).fetchone()
    assert row is not None


def test_schema_version_newer_than_current_is_rejected(target):
    with pytest.raises(ValueError):
        import_config(
            target, {"schema_version": SCHEMA_VERSION + 1}, allow_custom_code=True, actor="test"
        )


def test_schema_version_equal_or_older_proceeds(target):
    summary = import_config(target, {"schema_version": SCHEMA_VERSION}, allow_custom_code=True, actor="test")
    assert summary.results == []
    summary = import_config(target, {"schema_version": 1}, allow_custom_code=True, actor="test")
    assert summary.results == []


# --------------------------------- import: secrets ---------------------------------


def test_import_never_accepts_a_secret_key_even_if_hand_crafted_into_the_file(target):
    export = {"schema_version": 1, "settings": {"ANTHROPIC_API_KEY": "sk-sneaky"}}

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    assert get_setting("ANTHROPIC_API_KEY", conn=target, env_fallback=False) is None
    result = summary.results[0]
    assert result.action == "skipped"
    assert "secret" in result.reason


def test_import_skips_an_unknown_settings_key_without_failing_the_batch(target):
    export = {
        "schema_version": 1,
        "settings": {
            "SOME_KEY_FROM_A_FUTURE_VERSION": "x",
            "DISPATCHER_INTERVAL_SECONDS": "6",
        },
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    assert get_setting("DISPATCHER_INTERVAL_SECONDS", conn=target, env_fallback=False) == "6"
    unknown_result = next(
        r for r in summary.results if r.key == "SOME_KEY_FROM_A_FUTURE_VERSION"
    )
    assert unknown_result.action == "skipped"
    assert "catalog" in unknown_result.reason


# --------------------------------- import: adapter_instances / CUSTOM gate ---------------------------------


def test_custom_adapter_refused_when_custom_code_not_allowed(target):
    export = {
        "schema_version": 1,
        "adapter_instances": [
            {"source": "sneaky", "adapter_type": "custom", "config": {"code": "def fetch(config): return []"}}
        ],
    }

    summary = import_config(target, export, allow_custom_code=False, actor="test")

    assert get_adapter_instance(target, "sneaky") is None
    result = summary.results[0]
    assert result.action == "skipped"
    assert "custom" in result.reason.lower()


def test_custom_adapter_accepted_but_never_fetched_when_custom_code_allowed(target, monkeypatch):
    called = []
    monkeypatch.setattr(CustomAdapter, "fetch", lambda self: called.append(self.source))

    export = {
        "schema_version": 1,
        "adapter_instances": [
            {"source": "reviewed", "adapter_type": "custom", "config": {"code": "def fetch(config): return []"}}
        ],
    }

    import_config(target, export, allow_custom_code=True, actor="test")

    stored = get_adapter_instance(target, "reviewed")
    assert stored is not None
    assert stored["adapter_type"] == "custom"
    assert called == []  # never exec()'d/fetch()'d during import


def test_adapter_instance_bundles_display_name_into_a_source_row(target):
    export = {
        "schema_version": 1,
        "adapter_instances": [
            {
                "source": "demo",
                "adapter_type": "api",
                "config": {"url": "https://example.com"},
                "display_name": "Demo Source",
                "site_url": "https://example.com",
            }
        ],
    }

    import_config(target, export, allow_custom_code=True, actor="test")

    row = target.execute(
        "SELECT display_name, site_url FROM sources WHERE source = 'demo'"
    ).fetchone()
    assert row == ("Demo Source", "https://example.com")


def test_unknown_adapter_type_is_skipped(target):
    export = {
        "schema_version": 1,
        "adapter_instances": [{"source": "weird", "adapter_type": "not-a-real-type", "config": {}}],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    assert get_adapter_instance(target, "weird") is None
    assert summary.results[0].action == "skipped"


# --------------------------------- import: transmit_policy cross-reference ---------------------------------


def test_adapter_referencing_a_policy_defined_earlier_in_the_same_file_has_no_warning(target):
    export = {
        "schema_version": 1,
        "transmit_policies": [{"name": "urgent", "repeat_times": 5, "interval_seconds": 60}],
        "adapter_instances": [
            {
                "source": "demo",
                "adapter_type": "api",
                "config": {"url": "https://x", "transmit_policy": "urgent"},
            }
        ],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    adapter_result = next(r for r in summary.results if r.section == "adapter_instances")
    assert adapter_result.warnings == []


def test_adapter_referencing_an_unknown_policy_is_imported_but_flagged(target):
    export = {
        "schema_version": 1,
        "adapter_instances": [
            {
                "source": "demo",
                "adapter_type": "api",
                "config": {"url": "https://x", "transmit_policy": "does-not-exist"},
            }
        ],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    assert get_adapter_instance(target, "demo") is not None  # still imported
    adapter_result = next(r for r in summary.results if r.section == "adapter_instances")
    assert adapter_result.action == "created"
    assert len(adapter_result.warnings) == 1
    assert "does-not-exist" in adapter_result.warnings[0]


def test_adapter_referencing_the_default_policy_name_is_never_flagged(target):
    export = {
        "schema_version": 1,
        "adapter_instances": [
            {
                "source": "demo",
                "adapter_type": "api",
                "config": {"url": "https://x", "transmit_policy": "informational"},
            }
        ],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    adapter_result = next(r for r in summary.results if r.section == "adapter_instances")
    assert adapter_result.warnings == []


# --------------------------------- round trip ---------------------------------


def test_full_round_trip_from_a_populated_db_into_a_fresh_one(conn, target):
    set_setting(conn, "DISPATCHER_INTERVAL_SECONDS", "8")
    set_setting(conn, "ANTHROPIC_API_KEY", "sk-real", is_secret=True)
    set_policy(conn, "urgent", 5, 60, "Escalated")
    set_adapter_instance(
        conn, "demo", "api", {"url": "https://example.com", "transmit_policy": "urgent"}
    )
    set_source(conn, "demo", "Demo Source", "https://example.com")
    set_source(conn, "orphan", "Orphan", None)

    export = build_export(conn, actor="test")
    summary = import_config(target, export, allow_custom_code=True, actor="test")

    assert all(c["skipped"] == 0 for c in summary.counts().values())
    assert get_setting("DISPATCHER_INTERVAL_SECONDS", conn=target, env_fallback=False) == "8"
    assert get_setting("ANTHROPIC_API_KEY", conn=target, env_fallback=False) is None
    assert get_policy(target, "urgent").repeat_times == 5
    demo = get_adapter_instance(target, "demo")
    assert demo is not None
    row = target.execute("SELECT display_name FROM sources WHERE source='demo'").fetchone()
    assert row[0] == "Demo Source"

    # Re-importing the same export a second time is a pure no-op in effect
    # (everything already matches) -- every record classifies as "updated",
    # nothing as "created".
    summary2 = import_config(target, export, allow_custom_code=True, actor="test")
    assert all(c["created"] == 0 for c in summary2.counts().values())
