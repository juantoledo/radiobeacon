import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "data-adapters" / "src"))

from adapters.storage import (  # noqa: E402
    DEFAULT_DB_PATH,
    delete_source,
    get_connection,
    list_sources,
    set_source,
)

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage the sources table -- per-source display metadata "
        "(display_name, site_url) exposed as the {source_name}/{source_url} "
        "template placeholders in beacon's BEACON_FRAME_PREFIX/SUFFIX and "
        "BEACON_VOICE_PREFIX/SUFFIX/TEMPLATE. Seeded with csn/senapred on first run."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to radiobeacon.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List every source")

    set_parser = subparsers.add_parser("set", help="Create or replace a source's display metadata")
    set_parser.add_argument("source", help="The raw source key, e.g. csn, senapred")
    set_parser.add_argument("--display-name", required=True)
    set_parser.add_argument("--site-url")

    delete_parser = subparsers.add_parser("delete", help="Delete a source")
    delete_parser.add_argument("source")

    args = parser.parse_args()

    conn = get_connection(args.db)  # creates + seeds `sources` if this is the first run

    if args.command == "list":
        rows = list_sources(conn)
        if not rows:
            print("No sources defined.")
        for source, display_name, site_url in rows:
            print(f"{source}\tdisplay_name={display_name}\tsite_url={site_url or ''}")

    elif args.command == "set":
        set_source(conn, args.source, args.display_name, args.site_url)
        logger.info(
            "set source=%s display_name=%s site_url=%s",
            args.source, args.display_name, args.site_url,
        )

    elif args.command == "delete":
        deleted = delete_source(conn, args.source)
        if deleted:
            logger.info("deleted source=%s", args.source)
        else:
            logger.error("no source found for source=%s", args.source)
            sys.exit(1)

    conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
