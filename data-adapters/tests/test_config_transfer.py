import pytest

from adapters.config_transfer import (
    ADAPTER_EXPORT_KIND,
    ADAPTER_EXPORT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    build_adapter_export,
    build_export,
    import_adapter_export,
    import_config,
)
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
from adapters.policy import get_policy, set_policy

V = SCHEMA_VERSION
AV = ADAPTER_EXPORT_SCHEMA_VERSION


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


def _policy_entry(name, **over):
    base = {
        "name": name,
        "fetch_kind": "interval",
        "fetch_interval_seconds": 10,
        "fetch_cron": None,
        "process_mode": "on_new_data",
        "transmit_kind": "once",
        "transmit_count": 1,
        "transmit_interval_seconds": 0,
        "transmit_cron": None,
        "description": None,
    }
    base.update(over)
    return base


# --------------------------------- export ---------------------------------


def test_export_never_includes_a_secret_key(conn):
    set_setting(conn, "ANTHROPIC_API_KEY", "sk-real-key", is_secret=True)
    set_setting(conn, "DISPATCHER_INTERVAL_SECONDS", "9")

    export = build_export(conn, actor="test")

    assert "ANTHROPIC_API_KEY" not in export["settings"]
    assert export["settings"]["DISPATCHER_INTERVAL_SECONDS"] == "9"


def test_export_bundles_a_linked_source_into_its_adapter_and_lists_only_orphans_separately(conn):
    set_adapter_instance(conn, "demo", "api", {"url": "https://example.com"})
    set_source(conn, "demo", "Demo Source", "https://example.com")
    set_source(conn, "orphan", "Orphan Source", None)

    export = build_export(conn, actor="test")

    demo = next(a for a in export["adapter_instances"] if a["source"] == "demo")
    assert demo["display_name"] == "Demo Source"
    assert [s["source"] for s in export["sources"]] == ["orphan"]


def test_export_schema_version_matches_module_constant(conn):
    assert build_export(conn, actor="test")["schema_version"] == SCHEMA_VERSION


def test_export_includes_the_full_policy_shape(conn):
    set_policy(conn, "urgent", transmit_kind="interval", transmit_count=5, transmit_interval_seconds=60)
    export = build_export(conn, actor="test")
    urgent = next(p for p in export["policies"] if p["name"] == "urgent")
    assert urgent["transmit_kind"] == "interval"
    assert urgent["transmit_count"] == 5
    assert urgent["transmit_interval_seconds"] == 60


# --------------------------------- import: basics ---------------------------------


def test_import_upserts_without_touching_a_pre_existing_extra_row(conn, target):
    set_policy(target, "extra", description="not in the import file")
    export = {"schema_version": V, "policies": [_policy_entry("default")]}

    import_config(target, export, allow_custom_code=True, actor="test")

    assert get_policy(target, "extra") is not None


def test_import_classifies_created_vs_updated(conn, target):
    set_policy(target, "default", description="already here")
    export = {
        "schema_version": V,
        "policies": [
            _policy_entry("default"),
            _policy_entry("urgent", transmit_kind="interval", transmit_count=5,
                          transmit_interval_seconds=60),
        ],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    by_key = {r.key: r.action for r in summary.results if r.section == "policies"}
    assert by_key == {"default": "updated", "urgent": "created"}


def test_one_bad_record_does_not_abort_the_rest_of_the_batch(conn, target):
    export = {
        "schema_version": V,
        "policies": [
            _policy_entry("good"),
            _policy_entry("bad", fetch_kind="cron", fetch_cron="not a cron"),
            _policy_entry("also-good", transmit_interval_seconds=10),
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
        "schema_version": V,
        "settings": {"DISPATCHER_INTERVAL_SECONDS": "42"},
        "policies": [_policy_entry("urgent")],
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


def test_schema_version_newer_than_current_is_rejected(target):
    with pytest.raises(ValueError):
        import_config(
            target, {"schema_version": SCHEMA_VERSION + 1}, allow_custom_code=True, actor="test"
        )


def test_pre_v2_schema_version_is_rejected(target):
    with pytest.raises(ValueError):
        import_config(target, {"schema_version": 1}, allow_custom_code=True, actor="test")


# --------------------------------- import: secrets ---------------------------------


def test_import_never_accepts_a_secret_key_even_if_hand_crafted_into_the_file(target):
    export = {"schema_version": V, "settings": {"ANTHROPIC_API_KEY": "sk-sneaky"}}

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    assert get_setting("ANTHROPIC_API_KEY", conn=target, env_fallback=False) is None
    result = summary.results[0]
    assert result.action == "skipped"
    assert "secret" in result.reason


# --------------------------------- import: adapter_instances / CUSTOM gate ---------------------------------


def test_custom_adapter_refused_when_custom_code_not_allowed(target):
    export = {
        "schema_version": V,
        "adapter_instances": [
            {"source": "sneaky", "adapter_type": "custom",
             "config": {"code": "def fetch(config): return []"}}
        ],
    }

    summary = import_config(target, export, allow_custom_code=False, actor="test")

    assert get_adapter_instance(target, "sneaky") is None
    assert summary.results[0].action == "skipped"


def test_custom_adapter_accepted_but_never_fetched_when_custom_code_allowed(target, monkeypatch):
    called = []
    monkeypatch.setattr(CustomAdapter, "fetch", lambda self: called.append(self.source))

    export = {
        "schema_version": V,
        "adapter_instances": [
            {"source": "reviewed", "adapter_type": "custom",
             "config": {"code": "def fetch(config): return []"}}
        ],
    }

    import_config(target, export, allow_custom_code=True, actor="test")

    stored = get_adapter_instance(target, "reviewed")
    assert stored is not None
    assert called == []


def test_unknown_adapter_type_is_skipped(target):
    export = {
        "schema_version": V,
        "adapter_instances": [{"source": "weird", "adapter_type": "not-a-real-type", "config": {}}],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    assert get_adapter_instance(target, "weird") is None
    assert summary.results[0].action == "skipped"


# --------------------------------- import: policy cross-reference ---------------------------------


def test_adapter_referencing_a_policy_defined_earlier_in_the_same_file_has_no_warning(target):
    export = {
        "schema_version": V,
        "policies": [_policy_entry("urgent")],
        "adapter_instances": [
            {"source": "demo", "adapter_type": "api", "policy": "urgent",
             "config": {"url": "https://x"}}
        ],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    adapter_result = next(r for r in summary.results if r.section == "adapter_instances")
    assert adapter_result.warnings == []


def test_adapter_referencing_an_unknown_policy_is_imported_but_flagged(target):
    export = {
        "schema_version": V,
        "adapter_instances": [
            {"source": "demo", "adapter_type": "api", "policy": "does-not-exist",
             "config": {"url": "https://x"}}
        ],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    assert get_adapter_instance(target, "demo") is not None
    adapter_result = next(r for r in summary.results if r.section == "adapter_instances")
    assert adapter_result.action == "created"
    assert len(adapter_result.warnings) == 1
    assert "does-not-exist" in adapter_result.warnings[0]


def test_adapter_referencing_the_default_policy_name_is_never_flagged(target):
    export = {
        "schema_version": V,
        "adapter_instances": [
            {"source": "demo", "adapter_type": "api", "policy": "default",
             "config": {"url": "https://x"}}
        ],
    }

    summary = import_config(target, export, allow_custom_code=True, actor="test")

    adapter_result = next(r for r in summary.results if r.section == "adapter_instances")
    assert adapter_result.warnings == []


# --------------------------------- round trip ---------------------------------


def test_full_round_trip_from_a_populated_db_into_a_fresh_one(conn, target):
    set_setting(conn, "DISPATCHER_INTERVAL_SECONDS", "8")
    set_setting(conn, "ANTHROPIC_API_KEY", "sk-real", is_secret=True)
    set_policy(conn, "urgent", transmit_kind="interval", transmit_count=5,
               transmit_interval_seconds=60, description="Escalated")
    set_adapter_instance(conn, "demo", "api", {"url": "https://example.com"}, policy="urgent")
    set_source(conn, "demo", "Demo Source", "https://example.com")
    set_source(conn, "orphan", "Orphan", None)

    export = build_export(conn, actor="test")
    summary = import_config(target, export, allow_custom_code=True, actor="test")

    assert all(c["skipped"] == 0 for c in summary.counts().values())
    assert get_setting("DISPATCHER_INTERVAL_SECONDS", conn=target, env_fallback=False) == "8"
    assert get_setting("ANTHROPIC_API_KEY", conn=target, env_fallback=False) is None
    assert get_policy(target, "urgent").transmit_count == 5
    demo = get_adapter_instance(target, "demo")
    assert demo is not None and demo["policy"] == "urgent"
    row = target.execute("SELECT display_name FROM sources WHERE source='demo'").fetchone()
    assert row[0] == "Demo Source"

    summary2 = import_config(target, export, allow_custom_code=True, actor="test")
    assert all(c["created"] == 0 for c in summary2.counts().values())


# --------------------------------- single-adapter export ---------------------------------


def _adapter_export(**over):
    base = {
        "kind": ADAPTER_EXPORT_KIND,
        "schema_version": AV,
        "adapter": {"source": "demo", "adapter_type": "api", "config": {"url": "https://x"}},
    }
    base.update(over)
    return base


def test_build_adapter_export_has_expected_envelope_kind_and_schema_version(conn):
    set_adapter_instance(conn, "demo", "api", {"url": "https://example.com"})

    export = build_adapter_export(conn, "demo", actor="test")

    assert export["kind"] == ADAPTER_EXPORT_KIND
    assert export["schema_version"] == ADAPTER_EXPORT_SCHEMA_VERSION
    assert export["adapter"]["source"] == "demo"
    assert export["adapter"]["adapter_type"] == "api"
    assert export["adapter"]["config"] == {"url": "https://example.com"}


def test_build_adapter_export_includes_linked_display_name_and_site_url(conn):
    set_adapter_instance(conn, "demo", "api", {"url": "https://example.com"})
    set_source(conn, "demo", "Demo Source", "https://example.com")

    export = build_adapter_export(conn, "demo", actor="test")

    assert export["adapter"]["display_name"] == "Demo Source"
    assert export["adapter"]["site_url"] == "https://example.com"


def test_build_adapter_export_omits_display_name_when_no_linked_source(conn):
    set_adapter_instance(conn, "demo", "api", {"url": "https://example.com"})

    export = build_adapter_export(conn, "demo", actor="test")

    assert "display_name" not in export["adapter"]


def test_build_adapter_export_raises_for_unknown_source(conn):
    with pytest.raises(LookupError):
        build_adapter_export(conn, "does-not-exist", actor="test")


# --------------------------------- single-adapter import ---------------------------------


def test_import_adapter_export_round_trip_creates_on_a_fresh_target(conn, target):
    set_adapter_instance(conn, "demo", "api", {"url": "https://example.com"}, policy="urgent")
    set_source(conn, "demo", "Demo Source", "https://example.com")
    export = build_adapter_export(conn, "demo", actor="test")

    result = import_adapter_export(target, export, allow_custom_code=True, actor="test")

    assert result.action == "created"
    stored = get_adapter_instance(target, "demo")
    assert stored is not None
    assert stored["policy"] == "urgent"
    row = target.execute("SELECT display_name FROM sources WHERE source='demo'").fetchone()
    assert row[0] == "Demo Source"


def test_import_adapter_export_overwrites_existing_source_when_reimported(target):
    set_adapter_instance(target, "demo", "api", {"url": "https://old"})
    export = _adapter_export(adapter={"source": "demo", "adapter_type": "api", "config": {"url": "https://new"}})

    result = import_adapter_export(target, export, allow_custom_code=True, actor="test")

    assert result.action == "updated"
    assert get_adapter_instance(target, "demo")["config"] == '{"url": "https://new"}'


def test_import_adapter_export_dry_run_makes_no_writes(target):
    export = _adapter_export()

    result = import_adapter_export(target, export, allow_custom_code=True, actor="test", dry_run=True)

    assert result.action == "created"
    assert get_adapter_instance(target, "demo") is None


def test_import_adapter_export_rejects_wrong_kind(target):
    export = _adapter_export(kind="something.else")
    with pytest.raises(ValueError):
        import_adapter_export(target, export, allow_custom_code=True, actor="test")


def test_import_adapter_export_rejects_wrong_schema_version(target):
    export = _adapter_export(schema_version=AV + 1)
    with pytest.raises(ValueError):
        import_adapter_export(target, export, allow_custom_code=True, actor="test")


def test_import_adapter_export_override_source_imports_under_new_key(target):
    export = _adapter_export()

    result = import_adapter_export(
        target, export, allow_custom_code=True, actor="test", override_source="demo-copy"
    )

    assert result.key == "demo-copy"
    assert get_adapter_instance(target, "demo-copy") is not None
    assert get_adapter_instance(target, "demo") is None


def test_import_adapter_export_skips_custom_type_when_allow_custom_code_false(target):
    export = _adapter_export(
        adapter={"source": "sneaky", "adapter_type": "custom",
                 "config": {"code": "def fetch(config): return []"}}
    )

    result = import_adapter_export(target, export, allow_custom_code=False, actor="test")

    assert result.action == "skipped"
    assert get_adapter_instance(target, "sneaky") is None


def test_import_adapter_export_warns_on_unknown_policy_name(target):
    export = _adapter_export(
        adapter={"source": "demo", "adapter_type": "api", "policy": "does-not-exist",
                 "config": {"url": "https://x"}}
    )

    result = import_adapter_export(target, export, allow_custom_code=True, actor="test")

    assert result.action == "created"
    assert len(result.warnings) == 1
    assert "does-not-exist" in result.warnings[0]


def test_import_adapter_export_skips_when_config_is_not_an_object(target):
    export = _adapter_export(adapter={"source": "demo", "adapter_type": "api", "config": "not-an-object"})

    result = import_adapter_export(target, export, allow_custom_code=True, actor="test")

    assert result.action == "skipped"
    assert get_adapter_instance(target, "demo") is None
