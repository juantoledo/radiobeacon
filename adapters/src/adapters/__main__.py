import importlib
import inspect
import logging
import pkgutil

import adapters
from adapters.base import DataSourceAdapter

logger = logging.getLogger(__name__)


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
    for adapter_class in adapter_classes:
        reading = adapter_class().fetch_and_store()
        logger.info(
            "adapter=%s ok=%s source=%s",
            adapter_class.__name__,
            reading.ok,
            reading.source,
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
