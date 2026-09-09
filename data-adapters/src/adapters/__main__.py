import argparse
import json
import logging
import signal
import sqlite3
import threading
import time

from adapters.aiprompt_adapter import AiPromptAdapter
from adapters.api_adapter import ApiAdapter
from adapters.base import DataSourceAdapter
from adapters.custom_adapter import CustomAdapter
from adapters.logsetup import configure_logging, refresh_level
from adapters.policy import Policy, resolve_policy
from adapters.storage import DEFAULT_DB_PATH, get_connection, list_adapter_instances
from adapters.timeutil import utc_now

logger = logging.getLogger("adapters")
_sup = logging.getLogger("adapters.supervisor")

# A source's loop never sleeps longer than this between checks, whatever
# its fetch interval / cron period — so it re-reads its adapter row and
# Policy this often and a UI edit (e.g. "every 1h" -> "every 20s") takes
# effect within this window rather than after the old schedule's next
# wake. Fetches still only happen when actually due.
_LOOP_POLL_CAP_SECONDS = 15

# How often main()'s supervisor re-scans adapter_instances for sources that
# have been added / enabled while it was running, and spawns a loop thread
# for each. (A removed / disabled source's own loop notices and exits by
# itself — see _run_adapter_loop.)
_SUPERVISOR_POLL_SECONDS = 15

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
                "failed to build adapter, skipping source=%s type=%s",
                row["source"],
                row["adapter_type"],
                exc_info=True,
            )
            continue
        loaded.append((row["source"], adapter, policy))
    return loaded


def _reload_adapter(
    source: str, db_path
) -> tuple[DataSourceAdapter, Policy] | None:
    """Re-read a source's row and rebuild its adapter + resolve its Policy
    from the DB — called at the top of every loop iteration so an edit to
    the adapter's config *or* its Policy (via the /adapters and
    /config/policies pages) goes live with no restart. Returns None if the
    row has gone (deleted / disabled), or if the config is currently
    unbuildable (logged; the loop retries next iteration)."""
    conn = get_connection(db_path)
    try:
        row = next(
            (r for r in list_adapter_instances(conn) if r["source"] == source and r["enabled"]),
            None,
        )
        if row is None:
            return None
        try:
            policy = resolve_policy(conn, row["policy"])
            adapter = build_adapter(
                row["adapter_type"], source, json.loads(row["config"]), policy
            )
        except (ValueError, json.JSONDecodeError):
            logger.error("adapter row not buildable, skipping this cycle source=%s",
                         source, exc_info=True)
            return None
        return adapter, policy
    finally:
        conn.close()


def _latest_cron_occurrence(cron_expr: str, db_path):
    """The most recent cron occurrence at or before now, as a UTC datetime
    (the per-cycle dedup anchor), or None if the expression is invalid.
    Opens a short-lived connection for the display-timezone lookup."""
    from adapters.cron import latest_fire_at_or_before

    conn = get_connection(db_path)
    try:
        return latest_fire_at_or_before(cron_expr, utc_now(), conn=conn)
    finally:
        conn.close()


def _fetch_once(source: str, adapter: DataSourceAdapter) -> None:
    try:
        reading = adapter.fetch_and_store()
        logger.info("fetch done source=%s ok=%s", reading.source, reading.ok)
    except Exception:
        logger.error("unhandled error during fetch source=%s", source, exc_info=True)


def _run_adapter_loop(
    source: str,
    stop_event: threading.Event,
    *,
    db_path=DEFAULT_DB_PATH,
) -> None:
    """Runs one source forever, independently of every other source's loop.
    The adapter (config) and its Policy's fetch stage are re-read from the
    DB every cycle, and no cycle sleeps longer than _LOOP_POLL_CAP_SECONDS,
    so a UI edit — including a Policy change from a long interval/cron to a
    short one — takes effect within that window rather than after the old
    schedule's next wake. A fetch only fires when actually due:
    `interval` when that many seconds have elapsed since the last one,
    `cron` once per occurrence (with a catch-up on startup), `once` a
    single time. Adding/removing a whole adapter is the supervisor's job
    (see main); this loop just stops itself when its row is gone/disabled.
    """
    logger.info("adapter loop starting source=%s", source)
    last_fetch_at: float | None = None   # time.monotonic() of the last fetch
    last_cron_occurrence = None
    while not stop_event.is_set():
        reloaded = _reload_adapter(source, db_path)
        if reloaded is None:
            conn = get_connection(db_path)
            try:
                still_there = any(
                    r["source"] == source and r["enabled"] for r in list_adapter_instances(conn)
                )
            finally:
                conn.close()
            if not still_there:
                logger.info("adapter row gone, stopping loop source=%s", source)
                return
            stop_event.wait(_LOOP_POLL_CAP_SECONDS)   # unbuildable config -> retry
            continue

        adapter, policy = reloaded
        fetch = policy.fetch
        now = time.monotonic()

        if fetch.kind == "once":
            if last_fetch_at is None:
                _fetch_once(source, adapter)
                last_fetch_at = time.monotonic()
        elif fetch.kind == "cron":
            if not fetch.cron:
                logger.error("policy fetch is cron but has no expression, idling source=%s policy=%r",
                             source, policy.name)
            else:
                occ = _latest_cron_occurrence(fetch.cron, db_path)
                if last_fetch_at is None or (occ is not None and occ != last_cron_occurrence):
                    _fetch_once(source, adapter)
                    last_fetch_at = time.monotonic()
                    last_cron_occurrence = occ
        else:  # interval
            interval = max(1, fetch.interval_seconds or 10)
            if last_fetch_at is None or (now - last_fetch_at) >= interval:
                _fetch_once(source, adapter)
                last_fetch_at = time.monotonic()
                due_in = interval
            else:
                due_in = interval - (now - last_fetch_at)
            stop_event.wait(max(0.5, min(due_in, _LOOP_POLL_CAP_SECONDS)))
            continue

        stop_event.wait(_LOOP_POLL_CAP_SECONDS)
    logger.info("adapter loop stopped source=%s", source)


def _enabled_sources(db_path=DEFAULT_DB_PATH) -> list[str]:
    conn = get_connection(db_path)
    try:
        return [r["source"] for r in list_adapter_instances(conn) if r["enabled"]]
    finally:
        conn.close()


def _supervise(stop_event: threading.Event, *, db_path=DEFAULT_DB_PATH) -> None:
    """Keep one loop thread running per enabled source, re-scanned every
    _SUPERVISOR_POLL_SECONDS so a source added or enabled through the UI
    starts running with no process restart. A source that is removed or
    disabled has its own loop exit on its next iteration; this reaps the
    dead thread and will re-spawn it if the source comes back."""
    threads: dict[str, threading.Thread] = {}
    logged_empty = False
    while not stop_event.is_set():
        refresh_level(db_path=db_path)
        try:
            enabled = set(_enabled_sources(db_path))
        except Exception:
            _sup.error("failed to read adapter_instances, retrying", exc_info=True)
            enabled = set(threads)

        for source in list(threads):
            if not threads[source].is_alive():
                del threads[source]

        for source in sorted(enabled - set(threads)):
            thread = threading.Thread(
                target=_run_adapter_loop,
                args=(source, stop_event),
                kwargs={"db_path": db_path},
                name=source,
                daemon=True,
            )
            thread.start()
            threads[source] = thread
            _sup.info("started adapter loop source=%s", source)

        if not threads and not logged_empty:
            _sup.warning("no enabled adapter instances found, watching for new ones")
            logged_empty = True
        elif threads:
            logged_empty = False

        stop_event.wait(_SUPERVISOR_POLL_SECONDS)

    for thread in threads.values():
        thread.join()


def main() -> None:
    stop_event = threading.Event()

    def _handle_shutdown_signal(signum, frame) -> None:
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    _supervise(stop_event)


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
    logger.info("fetch done source=%s ok=%s error=%s", source, reading.ok, reading.error)


if __name__ == "__main__":
    configure_logging("adapters")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", metavar="SOURCE", help="fetch_and_store one instance once, then exit")
    args = parser.parse_args()
    if args.once:
        run_once(args.once)
    else:
        main()
