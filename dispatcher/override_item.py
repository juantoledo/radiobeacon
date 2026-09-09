import argparse
import logging
import sys

from adapters.storage import (
    DEFAULT_DB_PATH,
    get_connection,
    get_setting,
    register_audit_event_hook,
)
from dispatcher.mq_publisher import publish_cloud_event
from dispatcher.override import override_item, rearm_item

logger = logging.getLogger(__name__)

# Publishes select audit events (see dispatcher/mq_publisher.py) to MQTT as
# CloudEvents — on by default (DISPATCHER_MQ_HOST defaults to localhost),
# no-ops only when DISPATCHER_MQ_HOST is explicitly set empty.
register_audit_event_hook(publish_cloud_event)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-point an item at a different Policy (e.g. from the UI, or "
        "manually), and optionally re-arm it for delivery if it's already been "
        "delivered/retired."
    )
    parser.add_argument("source")
    parser.add_argument("item_id")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to radiobeacon.db")
    parser.add_argument(
        "--policy",
        help="Name of a row in `policies` (see dispatcher/policies.sh list)",
    )
    parser.add_argument(
        "--rearm",
        action="store_true",
        help="Re-enter delivery if this item has already been fully delivered/retired. "
        "Has no effect if the item is still in-flight (it's already picked up on the "
        "next poll automatically).",
    )
    parser.add_argument(
        "--consumer",
        default=get_setting("DISPATCHER_CONSUMER_NAME", "log"),
        help="Consumer to re-arm for (only relevant with --rearm)",
    )
    args = parser.parse_args()

    conn = get_connection(args.db)

    if args.policy is not None:
        updated = override_item(
            conn,
            args.source,
            args.item_id,
            policy=args.policy,
        )
        if not updated:
            logger.error("no item found for source=%s item_id=%s", args.source, args.item_id)
            sys.exit(1)
        logger.info("updated source=%s item_id=%s", args.source, args.item_id)

    if args.rearm:
        rearmed = rearm_item(conn, args.consumer, args.source, args.item_id)
        if rearmed:
            logger.info(
                "re-armed source=%s item_id=%s for consumer=%s",
                args.source,
                args.item_id,
                args.consumer,
            )
        else:
            logger.warning(
                "did not re-arm source=%s item_id=%s for consumer=%s "
                "(item doesn't exist, or is already in-flight)",
                args.source,
                args.item_id,
                args.consumer,
            )

    conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
