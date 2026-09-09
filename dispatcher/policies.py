import argparse
import logging
import sys

from adapters.storage import DEFAULT_DB_PATH, get_connection, register_audit_event_hook
from adapters.policy import delete_policy, describe_policy, list_policies, resolve_policy, set_policy
from dispatcher.mq_publisher import publish_cloud_event

logger = logging.getLogger(__name__)

# Publishes select audit events (see dispatcher/mq_publisher.py) to MQTT as
# CloudEvents — on by default (DISPATCHER_MQ_HOST defaults to localhost),
# no-ops only when DISPATCHER_MQ_HOST is explicitly set empty.
register_audit_event_hook(publish_cloud_event)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage the `policies` table — the single definition of how an "
        "item behaves (fetch cadence, process, transmit). adapter_instances.policy "
        "and items.policy reference a Policy by name."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to radiobeacon.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List every named Policy")

    set_parser = subparsers.add_parser("set", help="Create or replace a named Policy")
    set_parser.add_argument("name")
    set_parser.add_argument("--fetch-kind", choices=("once", "interval", "cron"), default="interval")
    set_parser.add_argument("--fetch-interval-seconds", type=int)
    set_parser.add_argument("--fetch-cron")
    set_parser.add_argument("--transmit-kind", choices=("once", "interval", "cron"), default="once")
    set_parser.add_argument("--transmit-count", type=int, default=1)
    set_parser.add_argument("--transmit-interval-seconds", type=int, default=0)
    set_parser.add_argument("--transmit-cron")
    set_parser.add_argument("--description")

    delete_parser = subparsers.add_parser("delete", help="Delete a named Policy")
    delete_parser.add_argument("name")

    args = parser.parse_args()

    conn = get_connection(args.db)  # creates + seeds `policies` on first run

    if args.command == "list":
        rows = list_policies(conn)
        if not rows:
            print("No policies defined.")
        for row in rows:
            summary = describe_policy(resolve_policy(conn, row.name))
            print(f"{row.name}\t{summary}\t{row.description or ''}")

    elif args.command == "set":
        set_policy(
            conn,
            args.name,
            fetch_kind=args.fetch_kind,
            fetch_interval_seconds=args.fetch_interval_seconds,
            fetch_cron=args.fetch_cron,
            transmit_kind=args.transmit_kind,
            transmit_count=args.transmit_count,
            transmit_interval_seconds=args.transmit_interval_seconds,
            transmit_cron=args.transmit_cron,
            description=args.description,
        )
        logger.info("set policy name=%s (%s)", args.name, describe_policy(resolve_policy(conn, args.name)))

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
