import logging

from . import CsnAdapter

logger = logging.getLogger(__name__)


def main() -> None:
    reading = CsnAdapter().fetch_and_store()
    logger.info("stored reading ok=%s source=%s", reading.ok, reading.source)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
