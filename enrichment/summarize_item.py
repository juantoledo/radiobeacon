import argparse
import logging
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "adapters" / "src"))
sys.path.insert(0, str(REPO_ROOT / "enrichment" / "src"))

from adapters.storage import DEFAULT_DB_PATH  # noqa: E402
from enrichment.sumarizer import summarize  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-summarize one stored item by id, bypassing the "
        "new-items-only skip that normal fetches use."
    )
    parser.add_argument("item_id")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to radiobeacon.db")
    parser.add_argument(
        "-n", "--sentences", type=int, default=2, help="Sentences to keep (default: 2)"
    )
    args = parser.parse_args()

    logger.info("looking up item_id=%s in %s", args.item_id, args.db)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM items WHERE item_id = ?", (args.item_id,)
    ).fetchone()
    if row is None:
        logger.error("item_id=%s not found in %s", args.item_id, args.db)
        sys.exit(1)

    fields = dict(row)
    logger.info(
        "found item_id=%s source=%s title=%r",
        args.item_id,
        fields["source"],
        fields["extracted_title"],
    )

    # Every column of the row is available to a custom ENRICHMENT_SUMARIZER_PROMPT as
    # a {column_name} placeholder — see enrichment/src/enrichment/sumarizer/__init__.py
    new_summary = summarize(fields, sentence_count=args.sentences)

    conn.execute(
        "UPDATE items SET summary = ? WHERE item_id = ?", (new_summary, args.item_id)
    )
    conn.commit()
    logger.info(
        "updated summary for item_id=%s (%d chars)",
        args.item_id,
        len(new_summary) if new_summary else 0,
    )

    print(f"item_id={args.item_id} source={fields['source']}")
    print("summary:")
    print(new_summary)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
