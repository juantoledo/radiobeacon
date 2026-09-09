"""Whole-DB config export/import — one JSON snapshot covering everything an
operator manages under /config: the settings catalog's overrides, transmit
policies, source display metadata, and per-source adapter definitions
(bundled with their matching `sources` row, since /config/adapters edits
both as one logical adapter — see ui.routers.adapters.adapter_save_action).

Both directions go through the exact same functions the UI/CLI already use
for a single hand edit (get_setting/set_setting, policy.set_policy,
set_source, set_adapter_instance) — so a bulk import is indistinguishable
from the same edits made one at a time, right down to the audit_log rows
and the optional MQTT publish (register_audit_event_hook). No raw SQL here.

Used identically by ui.routers.config_transfer and by
dispatcher/export_config.py + dispatcher/import_config.py — this module
itself has no UI/CLI-specific concept in it (e.g. `allow_custom_code` is a
plain bool the caller computes, not something this module reads settings
for itself).

Secrets (ANTHROPIC_API_KEY/OPENAI_API_KEY) are never written to an export,
not even as a placeholder key — matching set_setting's own "secret
plaintext never reaches audit_log" discipline. See settings_registry.py for
why the settings' type/choices metadata lives in its own small module here
rather than importing ui.config_catalog.SETTINGS_CATALOG (wrong dependency
direction)."""
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .settings_registry import SECRET_SETTING_KEYS, SETTINGS_REGISTRY
from .storage import (
    get_setting,
    list_adapter_instances,
    list_settings,
    list_sources,
    record_audit_event,
    set_adapter_instance,
    set_setting,
    set_source,
)
from .timeutil import utc_now
from .policy import DEFAULT_POLICY_NAME, list_policies, set_policy

SCHEMA_VERSION = 2

_SECTIONS = ("settings", "policies", "sources", "adapter_instances")
_ADAPTER_TYPES = ("api", "custom", "aiprompt")


@dataclass
class ImportItemResult:
    section: str
    key: str
    action: str  # "created" | "updated" | "skipped"
    warnings: list[str] = field(default_factory=list)
    reason: str | None = None  # populated when action == "skipped"


@dataclass
class ImportSummary:
    results: list[ImportItemResult]
    dry_run: bool

    def counts(self) -> dict[str, dict[str, int]]:
        """Per-section {created, updated, skipped, warned} — every section
        listed even if the import file's array/map for it was empty or
        absent, so a caller can print/render a stable 4-row table.
        "warned" counts results carrying a non-empty `warnings` list; it's
        orthogonal to action (a warned record was still created/updated,
        never skipped just for having a warning)."""
        out = {s: {"created": 0, "updated": 0, "skipped": 0, "warned": 0} for s in _SECTIONS}
        for r in self.results:
            out[r.section][r.action] += 1
            if r.warnings:
                out[r.section]["warned"] += 1
        return out


def build_export(conn: sqlite3.Connection, *, actor: str) -> dict[str, Any]:
    """Everything under /config, minus secrets, as one JSON-serializable
    dict. Reuses list_settings/list_policies/list_sources/
    list_adapter_instances — no hand-rolled SQL. Records one
    `config.exported` audit event (with per-section counts, not the
    payload itself) so /audit shows who pulled a full snapshot and when."""
    settings: dict[str, str] = {}
    for row in list_settings(conn):
        key, value, is_secret = row[0], row[1], row[2]
        if is_secret or key in SECRET_SETTING_KEYS:
            continue
        settings[key] = value

    policies = [
        {
            "name": p.name,
            "fetch_kind": p.fetch_kind,
            "fetch_interval_seconds": p.fetch_interval_seconds,
            "fetch_cron": p.fetch_cron,
            "process_mode": p.process_mode,
            "transmit_kind": p.transmit_kind,
            "transmit_count": p.transmit_count,
            "transmit_interval_seconds": p.transmit_interval_seconds,
            "transmit_cron": p.transmit_cron,
            "description": p.description,
        }
        for p in list_policies(conn)
    ]

    all_sources: dict[str, tuple[str, str | None]] = {
        source: (display_name, site_url) for source, display_name, site_url in list_sources(conn)
    }
    adapter_rows = list_adapter_instances(conn)
    linked_sources = {row["source"] for row in adapter_rows}

    adapter_instances = []
    for row in adapter_rows:
        entry: dict[str, Any] = {
            "source": row["source"],
            "adapter_type": row["adapter_type"],
            "enabled": bool(row["enabled"]),
            "policy": row["policy"],
            "config": json.loads(row["config"]),
        }
        linked = all_sources.get(row["source"])
        if linked is not None:
            entry["display_name"], entry["site_url"] = linked
        adapter_instances.append(entry)

    # Only sources with no matching adapter_instances row — an adapter's
    # own display_name/site_url already rode along above. list_sources()
    # is itself ORDER BY source, so this stays sorted too.
    orphan_sources = [
        {"source": source, "display_name": display_name, "site_url": site_url}
        for source, (display_name, site_url) in all_sources.items()
        if source not in linked_sources
    ]

    export = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": utc_now().isoformat(),
        "exported_by": actor,
        # Operator sanity metadata only — never read back by import_config.
        # BEACON_CALLSIGN is the one required, DB-only "who is this
        # install" field the schema already has; None on an unconfigured
        # install (get_setting's own env_fallback=False -> default).
        "source_install": get_setting("BEACON_CALLSIGN", None, conn=conn, env_fallback=False),
        "settings": settings,
        "policies": policies,
        "sources": orphan_sources,
        "adapter_instances": adapter_instances,
    }
    record_audit_event(
        conn,
        event_type="config.exported",
        actor=actor,
        details={
            "settings": len(settings),
            "policies": len(policies),
            "sources": len(orphan_sources),
            "adapter_instances": len(adapter_instances),
        },
    )
    return export


def _coerce_setting_value(raw: Any) -> str:
    """Every setting is stored as a string (get_setting/set_setting never
    cast); a hand-edited import file may still use a native JSON bool/
    number for readability, so accept that too."""
    if isinstance(raw, str):
        return raw
    if raw is True:
        return "true"
    if raw is False:
        return "false"
    return str(raw)


def _import_settings(
    conn: sqlite3.Connection, entries: dict[str, Any], *, actor: str, dry_run: bool
) -> list[ImportItemResult]:
    existing = {row[0] for row in list_settings(conn)}
    results: list[ImportItemResult] = []
    for key, raw in entries.items():
        # Each record's own try/except (mirrors storage.
        # per-row isolation): the
        # explicit checks below cover every anticipated bad-input shape
        # (secret key, unknown key, wrong type/choice) via a plain
        # `continue`; this is the safety net for anything they didn't
        # anticipate, so one surprising record never aborts the rest of
        # the section's import.
        try:
            if key in SECRET_SETTING_KEYS:
                results.append(
                    ImportItemResult(
                        "settings", key, "skipped", reason="secret keys are never imported"
                    )
                )
                continue
            spec = SETTINGS_REGISTRY.get(key)
            if spec is None:
                results.append(
                    ImportItemResult(
                        "settings", key, "skipped", reason="not in current settings catalog"
                    )
                )
                continue
            type_, choices = spec
            value = _coerce_setting_value(raw)
            if type_ == "int":
                try:
                    int(value)
                except (TypeError, ValueError):
                    results.append(
                        ImportItemResult(
                            "settings", key, "skipped", reason=f"{value!r} is not an int"
                        )
                    )
                    continue
            elif type_ == "float":
                try:
                    float(value)
                except (TypeError, ValueError):
                    results.append(
                        ImportItemResult(
                            "settings", key, "skipped", reason=f"{value!r} is not a float"
                        )
                    )
                    continue
            elif type_ == "select" and value not in choices:
                results.append(
                    ImportItemResult(
                        "settings", key, "skipped", reason=f"{value!r} not one of {choices}"
                    )
                )
                continue

            action = "updated" if key in existing else "created"
            if not dry_run:
                set_setting(conn, key, value, is_secret=False, actor=actor)
            results.append(ImportItemResult("settings", key, action))
        except Exception as e:  # noqa: BLE001 - see comment above
            results.append(ImportItemResult("settings", key, "skipped", reason=str(e)))
    return results


def _import_policies(
    conn: sqlite3.Connection, entries: list[Any], *, dry_run: bool
) -> tuple[list[ImportItemResult], set[str]]:
    existing = {p.name for p in list_policies(conn)}
    known = set(existing)  # grows as valid records are accepted, dry_run or not
    results: list[ImportItemResult] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else "?"
        try:
            if not isinstance(entry, dict):
                results.append(
                    ImportItemResult("policies", "?", "skipped", reason="not an object")
                )
                continue
            if not name:
                results.append(
                    ImportItemResult("policies", "?", "skipped", reason="missing 'name'")
                )
                continue

            action = "updated" if name in existing else "created"
            if not dry_run:
                # set_policy validates kinds / crons / counts and raises
                # ValueError on an incoherent record -> caught below.
                set_policy(
                    conn,
                    name,
                    fetch_kind=entry.get("fetch_kind", "interval"),
                    fetch_interval_seconds=entry.get("fetch_interval_seconds"),
                    fetch_cron=entry.get("fetch_cron"),
                    process_mode=entry.get("process_mode", "on_new_data"),
                    transmit_kind=entry.get("transmit_kind", "once"),
                    transmit_count=int(entry.get("transmit_count", 1)),
                    transmit_interval_seconds=int(entry.get("transmit_interval_seconds", 0)),
                    transmit_cron=entry.get("transmit_cron"),
                    description=entry.get("description"),
                )
            known.add(name)
            results.append(ImportItemResult("policies", name, action))
        except Exception as e:  # noqa: BLE001
            results.append(ImportItemResult("policies", name, "skipped", reason=str(e)))
    return results, known


def _import_sources(
    conn: sqlite3.Connection, entries: list[Any], *, dry_run: bool
) -> list[ImportItemResult]:
    existing = {row[0] for row in list_sources(conn)}
    results: list[ImportItemResult] = []
    for entry in entries:
        source = entry.get("source") if isinstance(entry, dict) else "?"
        try:
            if not isinstance(entry, dict):
                results.append(
                    ImportItemResult("sources", "?", "skipped", reason="not an object")
                )
                continue
            display_name = entry.get("display_name")
            if not source or not display_name:
                results.append(
                    ImportItemResult(
                        "sources", source or "?", "skipped",
                        reason="'source' and 'display_name' are both required",
                    )
                )
                continue
            action = "updated" if source in existing else "created"
            if not dry_run:
                set_source(conn, source, display_name, entry.get("site_url"))
            results.append(ImportItemResult("sources", source, action))
        except Exception as e:  # noqa: BLE001
            results.append(ImportItemResult("sources", source or "?", "skipped", reason=str(e)))
    return results


def _import_adapter_instances(
    conn: sqlite3.Connection,
    entries: list[Any],
    *,
    known_policy_names: set[str],
    allow_custom_code: bool,
    dry_run: bool,
) -> list[ImportItemResult]:
    existing = {row["source"] for row in list_adapter_instances(conn)}
    results: list[ImportItemResult] = []
    for entry in entries:
        source = entry.get("source") if isinstance(entry, dict) else "?"
        try:
            if not isinstance(entry, dict):
                results.append(
                    ImportItemResult("adapter_instances", "?", "skipped", reason="not an object")
                )
                continue
            if not source:
                results.append(
                    ImportItemResult(
                        "adapter_instances", "?", "skipped", reason="missing 'source'"
                    )
                )
                continue
            adapter_type = entry.get("adapter_type")
            if adapter_type not in _ADAPTER_TYPES:
                results.append(
                    ImportItemResult(
                        "adapter_instances", source, "skipped",
                        reason=f"adapter_type must be one of {_ADAPTER_TYPES}, got {adapter_type!r}",
                    )
                )
                continue
            # A pure string check — config is never read further, exec()'d,
            # or test-fetched here whether this branch is taken or not.
            if adapter_type == "custom" and not allow_custom_code:
                results.append(
                    ImportItemResult(
                        "adapter_instances", source, "skipped",
                        reason="custom adapter code refused: target's UI_DEV_TOOLS_ENABLED is off",
                    )
                )
                continue
            config = entry.get("config")
            if not isinstance(config, dict):
                results.append(
                    ImportItemResult(
                        "adapter_instances", source, "skipped",
                        reason="'config' must be an object",
                    )
                )
                continue
            policy_name = entry.get("policy")

            warnings: list[str] = []
            if (
                policy_name
                and policy_name != DEFAULT_POLICY_NAME
                and policy_name not in known_policy_names
            ):
                warnings.append(
                    f"policy {policy_name!r} not found on the target — items will "
                    f"fall back to {DEFAULT_POLICY_NAME!r} until a Policy with that name exists"
                )

            action = "updated" if source in existing else "created"
            if not dry_run:
                set_adapter_instance(
                    conn,
                    source,
                    adapter_type,
                    config,
                    enabled=bool(entry.get("enabled", True)),
                    policy=policy_name,
                )
                display_name = entry.get("display_name")
                if display_name:
                    set_source(conn, source, display_name, entry.get("site_url"))
            results.append(
                ImportItemResult("adapter_instances", source, action, warnings=warnings)
            )
        except Exception as e:  # noqa: BLE001
            results.append(
                ImportItemResult("adapter_instances", source or "?", "skipped", reason=str(e))
            )
    return results


def import_config(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    *,
    allow_custom_code: bool,
    actor: str,
    dry_run: bool = False,
) -> ImportSummary:
    """Upserts every record in `data` (an export_config.py/`/config/
    import-export` file, or a hand-edited one following the same shape)
    via the same set_setting/set_policy/set_source/set_adapter_instance
    functions a manual edit uses — never deletes anything absent from the
    file. Each record is validated and applied independently, in its own
    try/except, so one malformed record never aborts the import; it's just
    recorded as "skipped" with a reason.

    Order matters: settings, then policies, then sources, then
    adapter_instances — Policies land before adapters so an adapter's
    `policy` cross-reference can resolve against a Policy defined earlier
    in the *same* file, not just what predates it in the DB.

    dry_run=True runs every validation (including correct created-vs-
    updated classification, which only needs read-only lookups) but makes
    no writes — the DB is provably untouched. dry_run=False additionally
    records one `config.imported` audit event with the resulting
    ImportSummary.counts().

    Raises ValueError if `data["schema_version"]` is not exactly
    SCHEMA_VERSION — the Policy schema changed shape, so pre-v2 exports
    (flat `transmit_policies` / per-row `interval_seconds`) are no longer
    importable."""
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"config export schema_version {version!r} is not supported "
            f"(this build imports v{SCHEMA_VERSION} only)"
        )

    results: list[ImportItemResult] = []

    results.extend(
        _import_settings(conn, data.get("settings") or {}, actor=actor, dry_run=dry_run)
    )

    policy_results, known_policy_names = _import_policies(
        conn, data.get("policies") or [], dry_run=dry_run
    )
    results.extend(policy_results)

    results.extend(_import_sources(conn, data.get("sources") or [], dry_run=dry_run))

    results.extend(
        _import_adapter_instances(
            conn,
            data.get("adapter_instances") or [],
            known_policy_names=known_policy_names,
            allow_custom_code=allow_custom_code,
            dry_run=dry_run,
        )
    )

    summary = ImportSummary(results=results, dry_run=dry_run)
    if not dry_run:
        record_audit_event(
            conn, event_type="config.imported", actor=actor, details=summary.counts()
        )
    return summary
