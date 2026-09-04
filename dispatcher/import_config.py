import argparse
import json
import logging
import sys
from pathlib import Path

from adapters.config_transfer import import_config
from adapters.storage import DEFAULT_DB_PATH, get_connection, get_setting, register_audit_event_hook
from dispatcher.mq_publisher import publish_cloud_event

logger = logging.getLogger(__name__)

register_audit_event_hook(publish_cloud_event)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a JSON config export produced by export_config.py (or the "
        "/config/import-export UI page), upserting settings/transmit_policies/"
        "sources/adapter_instances by their natural key. Never deletes anything "
        "absent from the file. Secrets are never accepted, even if present in the "
        "file. A CUSTOM-type adapter is only imported when the target DB's "
        "UI_DEV_TOOLS_ENABLED is on (same gate the /config/adapters form applies "
        "to creating/editing one) — its code is never executed or test-run "
        "either way, only stored or skipped."
    )
    parser.add_argument("input", help="Path to a config export JSON file")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to radiobeacon.db")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate and print the summary; write nothing"
    )
    args = parser.parse_args()

    try:
        data = json.loads(Path(args.input).read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.error("could not read %s: %s", args.input, e)
        sys.exit(1)

    conn = get_connection(args.db)
    allow_custom_code = get_setting(
        "UI_DEV_TOOLS_ENABLED", "true", conn=conn
    ).lower() not in ("false", "0", "")

    try:
        summary = import_config(
            conn, data, allow_custom_code=allow_custom_code, actor="cli.import_config",
            dry_run=args.dry_run,
        )
    except ValueError as e:
        logger.error(str(e))
        conn.close()
        sys.exit(1)
    conn.close()

    for section, c in summary.counts().items():
        print(
            f"{section}: created={c['created']} updated={c['updated']} "
            f"skipped={c['skipped']} warned={c['warned']}"
        )
    for r in summary.results:
        if r.action == "skipped":
            print(f"  SKIP  {r.section}/{r.key}: {r.reason}")
        for w in r.warnings:
            print(f"  WARN  {r.section}/{r.key}: {w}")

    if args.dry_run:
        print("(dry run -- nothing was written)")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
