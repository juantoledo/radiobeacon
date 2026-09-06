import argparse
import json
import logging
import sys
from pathlib import Path

from adapters.config_transfer import build_export
from adapters.storage import DEFAULT_DB_PATH, get_connection, register_audit_event_hook
from dispatcher.mq_publisher import publish_cloud_event

logger = logging.getLogger(__name__)

# Publishes select audit events (see dispatcher/mq_publisher.py) to MQTT as
# CloudEvents — on by default (DISPATCHER_MQ_HOST defaults to localhost),
# no-ops only when DISPATCHER_MQ_HOST is explicitly set empty. Same
# one-liner dispatcher/policies.py and dispatcher/override_item.py already
# do — build_export records a `config.exported` audit event, which this
# then also gets published for, same as any other config change.
register_audit_event_hook(publish_cloud_event)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export the whole DB-backed config (settings, adapter_instances "
        "+ their sources, policies) to a JSON file. Secrets "
        "(ANTHROPIC_API_KEY/OPENAI_API_KEY) are always excluded, never even as a "
        "placeholder key. Counterpart to import_config.py; see "
        "adapters.config_transfer for the shared shape/logic both this and the "
        "/config/import-export UI page use."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to radiobeacon.db")
    parser.add_argument(
        "-o", "--output", help="Write to this path instead of printing to stdout"
    )
    args = parser.parse_args()

    conn = get_connection(args.db)
    export = build_export(conn, actor="cli.export_config")
    conn.close()

    text = json.dumps(export, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
        logger.info("wrote config export to %s", args.output)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
