import logging
import os
import signal
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "adapters" / "src"))

from adapters.storage import DEFAULT_DB_PATH, get_connection  # noqa: E402

from .watcher import check_for_new_items, log_handler  # noqa: E402

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = int(os.environ.get("DISPATCHER_INTERVAL_SECONDS", "5"))
CONSUMER_NAME = os.environ.get("DISPATCHER_CONSUMER_NAME", "log")

# The extension point: real handlers (radio TX, notifications, etc.) get
# added here once they exist — none do yet, so this just logs.
HANDLERS = [log_handler]


def main() -> None:
    conn = get_connection(DEFAULT_DB_PATH)
    stop_event = threading.Event()

    def _handle_shutdown_signal(signum, frame) -> None:
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    logger.info(
        "dispatcher: starting (consumer=%s, interval=%ds)", CONSUMER_NAME, INTERVAL_SECONDS
    )
    try:
        while not stop_event.is_set():
            try:
                dispatched = check_for_new_items(conn, CONSUMER_NAME, HANDLERS)
                if dispatched:
                    logger.info(
                        "dispatched %d item%s",
                        dispatched,
                        "" if dispatched == 1 else "s",
                    )
            except Exception:
                logger.error("unhandled error while checking for new items", exc_info=True)
            stop_event.wait(INTERVAL_SECONDS)
    finally:
        conn.close()
    logger.info("dispatcher: stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
