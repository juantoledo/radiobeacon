import importlib
import inspect
import logging
import pkgutil
import signal
import threading

import adapters
from adapters.base import DataSourceAdapter
from adapters.storage import get_setting

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_ENV_VAR = "ADAPTERS_DEFAULT_INTERVAL_SECONDS"
DEFAULT_INTERVAL_SECONDS = 600


def discover_adapters() -> list[type[DataSourceAdapter]]:
    """Finds every concrete DataSourceAdapter subclass in this package's
    submodules, so new adapters (each implementing the common contract in
    base.py) are picked up automatically without editing this file."""
    classes: list[type[DataSourceAdapter]] = []
    for module_info in pkgutil.iter_modules(adapters.__path__, adapters.__name__ + "."):
        module = importlib.import_module(module_info.name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, DataSourceAdapter)
                and obj is not DataSourceAdapter
                and not inspect.isabstract(obj)
            ):
                classes.append(obj)
    return list(dict.fromkeys(classes))


def _interval_seconds(adapter_class: type[DataSourceAdapter]) -> int:
    """Each adapter polls on its own schedule, configured alongside its
    other ADAPTERS_<NAME>_* env vars (e.g. ADAPTERS_SENAPRED_INTERVAL_SECONDS),
    derived from the adapter's module name so adding a new adapter doesn't
    require touching this file. Falls back to ADAPTERS_DEFAULT_INTERVAL_SECONDS
    (600s) if the adapter-specific var isn't set."""
    module_leaf = adapter_class.__module__.rsplit(".", 1)[-1].upper()
    adapter_env_var = f"ADAPTERS_{module_leaf}_INTERVAL_SECONDS"
    value = get_setting(adapter_env_var) or get_setting(DEFAULT_INTERVAL_ENV_VAR)
    return int(value) if value else DEFAULT_INTERVAL_SECONDS


def _run_adapter_loop(
    adapter_class: type[DataSourceAdapter], stop_event: threading.Event
) -> None:
    """Runs one adapter forever on its own interval, independently of every
    other adapter's loop. A failure in one iteration (fetch_and_store
    already turns network/parse errors into SourceReading.ok=False, but
    this is a last-resort guard for anything else, e.g. a storage error)
    is logged and the loop continues rather than killing this adapter's
    thread permanently."""
    interval = _interval_seconds(adapter_class)
    logger.info(
        "%s: starting loop (interval=%ds)", adapter_class.__name__, interval
    )
    while not stop_event.is_set():
        try:
            reading = adapter_class().fetch_and_store()
            logger.info(
                "adapter=%s ok=%s source=%s",
                adapter_class.__name__,
                reading.ok,
                reading.source,
            )
        except Exception:
            logger.error(
                "%s: unhandled error during fetch_and_store",
                adapter_class.__name__,
                exc_info=True,
            )
        stop_event.wait(interval)
    logger.info("%s: stopped", adapter_class.__name__)


def main() -> None:
    adapter_classes = discover_adapters()
    if not adapter_classes:
        logger.warning("no adapters found")
        return

    logger.info(
        "discovered %d adapter(s): %s",
        len(adapter_classes),
        ", ".join(cls.__name__ for cls in adapter_classes),
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
            args=(adapter_class, stop_event),
            name=adapter_class.__name__,
            daemon=True,
        )
        for adapter_class in adapter_classes
    ]
    for thread in threads:
        thread.start()

    # Block here (not just stop_event.wait()) so join() actually reaps each
    # thread once it finishes its current iteration and exits.
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
