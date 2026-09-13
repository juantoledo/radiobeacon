"""Resolves the whole-project version from the repo-root VERSION file —
the single source of truth every component's pyproject.toml and the UI
footer read from, bumped only by release.sh. Walks up from this file's own
location rather than assuming a fixed relative depth, so it keeps working
regardless of how deep an editable install places
data-adapters/src/adapters/ under the repo root."""
from pathlib import Path

_FALLBACK = "0.0.0-unknown"


def _read_version() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "VERSION"
        if candidate.is_file():
            return candidate.read_text().strip()
    return _FALLBACK


__version__ = _read_version()
