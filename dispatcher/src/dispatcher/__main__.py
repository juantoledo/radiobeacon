import logging
import signal
import threading

from adapters.storage import (
    DEFAULT_DB_PATH,
    get_connection,
    get_setting,
    register_audit_event_hook,
)
from adapters.timeutil import utc_now

from .mq_publisher import publish_cloud_event
from .watcher import check_for_new_items, log_handler

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = int(get_setting("DISPATCHER_INTERVAL_SECONDS", "5"))
CONSUMER_NAME = get_setting("DISPATCHER_CONSUMER_NAME", "log")

# The extension point: real handlers (radio TX, notifications, etc.) get
# added here once they exist — none do yet, so this just logs.
HANDLERS = [log_handler]

# Publishes select audit events (see mq_publisher.py) to MQTT as
# CloudEvents — on by default (DISPATCHER_MQ_HOST defaults to localhost),
# no-ops only when DISPATCHER_MQ_HOST is explicitly set empty.
register_audit_event_hook(publish_cloud_event)


def main() -> None:
    conn = get_connection(DEFAULT_DB_PATH)
    stop_event = threading.Event()

    def _handle_shutdown_signal(signum, frame) -> None:
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    # Captured once, at process start: an item whose own source_date_time
    # (the real-world event time, not when it was fetched/inserted)
    # predates this is never dispatched — see watcher.discover_new_items'
    # docstring. Not a rolling window, so a genuinely new event is never
    # excluded no matter how long this process keeps running.
    startup_time = utc_now()

    logger.info(
        "dispatcher: starting (consumer=%s, interval=%ds, ignoring source_date_time before %s)",
        CONSUMER_NAME, INTERVAL_SECONDS, startup_time.isoformat(),
    )
    try:
        while not stop_event.is_set():
            try:
                dispatched = check_for_new_items(
                    conn, CONSUMER_NAME, HANDLERS, not_before=startup_time
                )
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
