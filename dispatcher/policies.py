import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "data-adapters" / "src"))
sys.path.insert(0, str(REPO_ROOT / "dispatcher" / "src"))

from adapters.storage import DEFAULT_DB_PATH, get_connection, register_audit_event_hook  # noqa: E402
from adapters.transmit_policy import delete_policy, list_policies, set_policy  # noqa: E402
from dispatcher.mq_publisher import publish_cloud_event  # noqa: E402

logger = logging.getLogger(__name__)

# Publishes select audit events (see dispatcher/mq_publisher.py) to MQTT as
# CloudEvents — on by default (DISPATCHER_MQ_HOST defaults to localhost),
# no-ops only when DISPATCHER_MQ_HOST is explicitly set empty.
register_audit_event_hook(publish_cloud_event)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage the transmit_policies table — the centralized "
        "repeat_times/interval_seconds config that items' transmit_policy column "
        "references by name (applied by beacon/ when putting an item on air)."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to radiobeacon.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List every named policy")

    set_parser = subparsers.add_parser("set", help="Create or replace a named policy")
    set_parser.add_argument("name")
    set_parser.add_argument("--repeat-times", type=int, required=True)
    set_parser.add_argument("--interval-seconds", type=int, required=True)
    set_parser.add_argument("--description")

    delete_parser = subparsers.add_parser("delete", help="Delete a named policy")
    delete_parser.add_argument("name")

    args = parser.parse_args()

    conn = get_connection(args.db)  # creates + seeds transmit_policies on first run

    if args.command == "list":
        rows = list_policies(conn)
        if not rows:
            print("No policies defined.")
        for name, repeat_times, interval_seconds, description in rows:
            print(f"{name}\trepeat_times={repeat_times}\tinterval_seconds={interval_seconds}\t{description or ''}")

    elif args.command == "set":
        set_policy(
            conn,
            args.name,
            args.repeat_times,
            args.interval_seconds,
            description=args.description,
        )
        logger.info(
            "set policy name=%s repeat_times=%d interval_seconds=%d",
            args.name,
            args.repeat_times,
            args.interval_seconds,
        )

    elif args.command == "delete":
        deleted = delete_policy(conn, args.name)
        if deleted:
            logger.info("deleted policy name=%s", args.name)
        else:
            logger.error("no policy found for name=%s", args.name)
            sys.exit(1)

    conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
