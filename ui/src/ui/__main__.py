import uvicorn
from adapters.logsetup import configure_logging
from adapters.storage import DEFAULT_DB_PATH

from .app import app
from .config import UI_DB_PATH, UI_HOST, UI_PORT


def main() -> None:
    # Install the shared root logging config (format + LOG_LEVEL) before
    # uvicorn starts; log_config=None stops uvicorn replacing it, so its
    # own uvicorn.error / uvicorn.access records propagate through our one
    # handler and formatter too. The running server re-reads LOG_LEVEL on
    # a timer — see ui.app._lifespan.
    configure_logging("ui", db_path=UI_DB_PATH or DEFAULT_DB_PATH)
    uvicorn.run(app, host=UI_HOST, port=UI_PORT, log_config=None)


if __name__ == "__main__":
    main()
