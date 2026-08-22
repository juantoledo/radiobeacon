import uvicorn

from .app import app
from .config import UI_HOST, UI_PORT


def main() -> None:
    uvicorn.run(app, host=UI_HOST, port=UI_PORT)


if __name__ == "__main__":
    main()
