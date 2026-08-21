import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "adapters" / "src"))
sys.path.insert(0, str(REPO_ROOT / "dispatcher" / "src"))

from adapters.storage import DEFAULT_DB_PATH, get_connection  # noqa: E402
from dispatcher.override import override_item, rearm_item  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Override an item's dispatch_policy (e.g. from a future UI, or "
        "manually), and optionally re-arm it for delivery if it's already been fully "
        "delivered/retired."
    )
    parser.add_argument("source")
    parser.add_argument("item_id")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to radiobeacon.db")
    parser.add_argument(
        "--dispatch-policy",
        help="Name of a row in dispatch_policies (see dispatcher/policies.sh list)",
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
        default=os.environ.get("DISPATCHER_CONSUMER_NAME", "log"),
        help="Consumer to re-arm for (only relevant with --rearm)",
    )
    args = parser.parse_args()

    conn = get_connection(args.db)

    if args.dispatch_policy is not None:
        updated = override_item(
            conn,
            args.source,
            args.item_id,
            dispatch_policy=args.dispatch_policy,
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
