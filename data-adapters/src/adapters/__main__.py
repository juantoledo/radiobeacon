import argparse
import json
import logging
import signal
import sqlite3
import threading

from adapters.aiprompt_adapter import AiPromptAdapter
from adapters.api_adapter import ApiAdapter
from adapters.base import DataSourceAdapter
from adapters.custom_adapter import CustomAdapter
from adapters.storage import DEFAULT_DB_PATH, get_connection, get_setting, list_adapter_instances

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_ENV_VAR = "ADAPTERS_DEFAULT_INTERVAL_SECONDS"
DEFAULT_INTERVAL_SECONDS = 10

_ADAPTER_CLASSES = {"api": ApiAdapter, "custom": CustomAdapter, "aiprompt": AiPromptAdapter}


def build_adapter(adapter_type: str, source: str, config: dict) -> DataSourceAdapter:
    adapter_class = _ADAPTER_CLASSES.get(adapter_type)
    if adapter_class is None:
        raise ValueError(f"unknown adapter_type {adapter_type!r}")
    return adapter_class(source=source, config=config)


def load_enabled_adapters(
    conn: sqlite3.Connection,
) -> list[tuple[str, DataSourceAdapter, int]]:
    """Reads every enabled row from adapter_instances and builds its
    adapter — the DB-driven replacement for the old filesystem
    discover_adapters() scan. Returns (source, adapter, interval_seconds)
    tuples; interval_seconds falls back to ADAPTERS_DEFAULT_INTERVAL_SECONDS
    (unchanged default, still read via get_setting) when a row's own
    interval_seconds is NULL. A row whose adapter_type is unrecognized (or
    whose config JSON is malformed) is logged and skipped rather than
    aborting every other adapter."""
    default_interval_raw = get_setting(DEFAULT_INTERVAL_ENV_VAR, conn=conn)
    default_interval = int(default_interval_raw) if default_interval_raw else DEFAULT_INTERVAL_SECONDS

    loaded: list[tuple[str, DataSourceAdapter, int]] = []
    for row in list_adapter_instances(conn):
        if not row["enabled"]:
            continue
        try:
            config = json.loads(row["config"])
            adapter = build_adapter(row["adapter_type"], row["source"], config)
        except (ValueError, json.JSONDecodeError):
            logger.error(
                "source=%s: failed to build adapter (type=%s), skipping",
                row["source"],
                row["adapter_type"],
                exc_info=True,
            )
            continue
        interval = row["interval_seconds"] or default_interval
        loaded.append((row["source"], adapter, interval))
    return loaded


def _run_adapter_loop(
    source: str, adapter: DataSourceAdapter, interval: int, stop_event: threading.Event
) -> None:
    """Runs one adapter forever on its own interval, independently of every
    other adapter's loop — same shape as the old class-based scheduler, just
    parameterized by a DB row instead of a discovered class."""
    logger.info("%s: starting loop (interval=%ds)", source, interval)
    while not stop_event.is_set():
        try:
            reading = adapter.fetch_and_store()
            logger.info("adapter=%s ok=%s source=%s", source, reading.ok, reading.source)
        except Exception:
            logger.error("%s: unhandled error during fetch_and_store", source, exc_info=True)
        stop_event.wait(interval)
    logger.info("%s: stopped", source)


def main() -> None:
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        loaded = load_enabled_adapters(conn)
    finally:
        conn.close()

    if not loaded:
        logger.warning("no enabled adapter instances found")
        return

    logger.info(
        "loaded %d adapter instance(s): %s",
        len(loaded),
        ", ".join(source for source, _, _ in loaded),
    )

    stop_event = threading.Event()

    def _handle_shutdown_signal(signum, frame) -> None:
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    threads = [
        threading.Thread(
            target=_run_adapter_loop,
            args=(source, adapter, interval, stop_event),
            name=source,
            daemon=True,
        )
        for source, adapter, interval in loaded
    ]
    for thread in threads:
        thread.start()

    # Block here (not just stop_event.wait()) so join() actually reaps each
    # thread once it finishes its current iteration and exits.
    for thread in threads:
        thread.join()


def run_once(source: str) -> None:
    """`python -m adapters --once <source>` — fetches and stores once for a
    single configured instance and exits. Replaces the old per-adapter
    one-shot runners (adapters.csn.__main__/adapters.senapred.__main__),
    which no longer exist now that adapters are DB-configured rather than
    hardcoded modules."""
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        loaded = {src: (adapter, interval) for src, adapter, interval in load_enabled_adapters(conn)}
    finally:
        conn.close()
    if source not in loaded:
        raise SystemExit(f"no enabled adapter instance named {source!r}")
    adapter, _ = loaded[source]
    reading = adapter.fetch_and_store()
    logger.info("adapter=%s ok=%s error=%s", source, reading.ok, reading.error)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", metavar="SOURCE", help="fetch_and_store one instance once, then exit")
    args = parser.parse_args()
    if args.once:
        run_once(args.once)
    else:
        main()
