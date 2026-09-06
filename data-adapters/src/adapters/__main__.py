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
from adapters.policy import Policy, resolve_policy
from adapters.storage import DEFAULT_DB_PATH, get_connection, list_adapter_instances
from adapters.timeutil import utc_now

logger = logging.getLogger(__name__)

# Upper bound on how long a single loop iteration sleeps — so a long
# cron wait survives a clock jump / suspend and picks up a Policy edit
# within the window, and so an `once` adapter still notices a later
# once->interval Policy change.
_MAX_SLEEP_SECONDS = 3600

_ADAPTER_CLASSES = {"api": ApiAdapter, "custom": CustomAdapter, "aiprompt": AiPromptAdapter}


def build_adapter(
    adapter_type: str, source: str, config: dict, policy: Policy | None = None
) -> DataSourceAdapter:
    adapter_class = _ADAPTER_CLASSES.get(adapter_type)
    if adapter_class is None:
        raise ValueError(f"unknown adapter_type {adapter_type!r}")
    return adapter_class(source=source, config=config, policy=policy)


def load_enabled_adapters(
    conn: sqlite3.Connection,
) -> list[tuple[str, DataSourceAdapter, Policy]]:
    """Reads every enabled row from adapter_instances and builds its
    adapter, resolving its Policy (adapter_instances.policy -> the
    `policies` table). Returns (source, adapter, Policy) tuples. A row
    whose adapter_type is unrecognized (or whose config JSON is malformed)
    is logged and skipped rather than aborting every other adapter."""
    loaded: list[tuple[str, DataSourceAdapter, Policy]] = []
    for row in list_adapter_instances(conn):
        if not row["enabled"]:
            continue
        try:
            config = json.loads(row["config"])
            policy = resolve_policy(conn, row["policy"])
            adapter = build_adapter(row["adapter_type"], row["source"], config, policy)
        except (ValueError, json.JSONDecodeError):
            logger.error(
                "source=%s: failed to build adapter (type=%s), skipping",
                row["source"],
                row["adapter_type"],
                exc_info=True,
            )
            continue
        loaded.append((row["source"], adapter, policy))
    return loaded


def _resolve_current_policy(source: str, db_path) -> Policy | None:
    """Re-read a source's Policy — called at the top of every loop
    iteration so a Policy edit (value or kind) goes live with no restart.
    Returns None if the adapter row has gone (deleted / disabled)."""
    conn = get_connection(db_path)
    try:
        row = next(
            (r for r in list_adapter_instances(conn) if r["source"] == source and r["enabled"]),
            None,
        )
        if row is None:
            return None
        return resolve_policy(conn, row["policy"])
    finally:
        conn.close()


def _fetch_once(source: str, adapter: DataSourceAdapter) -> None:
    try:
        reading = adapter.fetch_and_store()
        logger.info("adapter=%s ok=%s source=%s", source, reading.ok, reading.source)
    except Exception:
        logger.error("%s: unhandled error during fetch_and_store", source, exc_info=True)


def _run_adapter_loop(
    source: str,
    adapter: DataSourceAdapter,
    stop_event: threading.Event,
    *,
    db_path=DEFAULT_DB_PATH,
) -> None:
    """Runs one adapter forever, on the cadence of its assigned Policy's
    fetch stage, independently of every other adapter's loop. The Policy
    is re-resolved each iteration so an edit takes effect without a
    restart. `interval` sleeps that many seconds; `cron` fetches once on
    start (catch-up) then sleeps until the next occurrence (capped);
    `once` fetches once then idles."""
    logger.info("%s: starting loop", source)
    first = True
    while not stop_event.is_set():
        policy = _resolve_current_policy(source, db_path)
        if policy is None:
            logger.info("%s: adapter row gone, stopping loop", source)
            return
        fetch = policy.fetch

        if fetch.kind == "cron":
            if not fetch.cron:
                logger.error("%s: Policy %r fetch is cron but has no expression; idling",
                             source, policy.name)
                stop_event.wait(_MAX_SLEEP_SECONDS)
                continue
            if first:
                _fetch_once(source, adapter)
            nxt = fetch.next_fire_after(utc_now(), conn=get_connection(db_path))
            if nxt is None:
                stop_event.wait(_MAX_SLEEP_SECONDS)
            else:
                wait_s = (nxt - utc_now()).total_seconds()
                stop_event.wait(max(1, min(wait_s, _MAX_SLEEP_SECONDS)))
                if not stop_event.is_set() and (nxt - utc_now()).total_seconds() <= 1:
                    _fetch_once(source, adapter)
        elif fetch.kind == "once":
            if first:
                _fetch_once(source, adapter)
            stop_event.wait(_MAX_SLEEP_SECONDS)
        else:  # interval
            _fetch_once(source, adapter)
            stop_event.wait(max(1, min(fetch.interval_seconds or 10, _MAX_SLEEP_SECONDS)))
        first = False
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
            args=(source, adapter, stop_event),
            name=source,
            daemon=True,
        )
        for source, adapter, _ in loaded
    ]
    for thread in threads:
        thread.start()

    # Block here (not just stop_event.wait()) so join() actually reaps each
    # thread once it finishes its current iteration and exits.
    for thread in threads:
        thread.join()


def run_once(source: str) -> None:
    """`python -m adapters --once <source>` — fetches and stores once for a
    single configured instance and exits, ignoring the Policy's fetch
    schedule."""
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        loaded = {src: adapter for src, adapter, _ in load_enabled_adapters(conn)}
    finally:
        conn.close()
    if source not in loaded:
        raise SystemExit(f"no enabled adapter instance named {source!r}")
    reading = loaded[source].fetch_and_store()
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
