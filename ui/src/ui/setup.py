"""The first-run setup wizard — a linear walk through the settings a fresh
install actually needs to look at before going on air: beacon identity
(reusing ui.beacon's existing required-field group), transmission mode, the
WAV hand-off to SvxLink (BEACON_WAV_TRANSMITTER's "logging" vs "spool"
choice), the TTS engine, the on-air watermark, AI summarization, and
display/locale defaults.

Completion is tracked by a single sticky DB flag (SETUP_WIZARD_COMPLETED,
see config_catalog.py) rather than by re-deriving "are all these fields
non-blank" the way is_beacon_configured() does for identity alone — several
wizard steps (BEACON_WAV_TRANSMITTER chief among them) already have a
perfectly good permanent default, so "has the operator seen and clicked
through this" is a different, and simpler, question than "are these fields
populated". Once set, the flag never auto-clears; an admin reopens the
wizard voluntarily via the sidebar link in base.html.

SetupRequired/require_setup_complete mirror current_user.NotAuthenticated's
shape exactly, including the reason a FastAPI *dependency* (not ASGI
middleware) is required: is_setup_complete needs DB access, and middleware
runs before Depends(get_db) resolves, so it can't reuse request.state.db_conn
(or a test's get_db override) the way a dependency can."""
import sqlite3
from dataclasses import dataclass

from adapters.auth import User
from adapters.storage import get_setting, set_setting
from fastapi import Depends

from .beacon import REQUIRED_BEACON_KEYS
from .current_user import get_current_user
from .db import get_db


def is_setup_complete(conn: sqlite3.Connection) -> bool:
    return get_setting(
        "SETUP_WIZARD_COMPLETED", "false", conn=conn, env_fallback=False
    ).lower() == "true"


def mark_setup_complete(conn: sqlite3.Connection, actor: str = "ui.setup") -> None:
    set_setting(conn, "SETUP_WIZARD_COMPLETED", "true", actor=actor)


class SetupRequired(Exception):
    """Raised by require_setup_complete when an admin hits a gated route
    before finishing the wizard; caught by an app.py exception handler that
    303-redirects to /setup."""


def require_setup_complete(
    user: User = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)
) -> None:
    """Router-level dependency (alongside each gated router's own
    require_role/get_current_user) — only ever blocks an admin, matching the
    existing beacon-identity banner's admin-only nagging: a plain 'user'
    role can't reach /setup or fix anything there anyway, so gating them too
    would just be a dead end."""
    if user.role == "admin" and not is_setup_complete(conn):
        raise SetupRequired()


@dataclass(frozen=True)
class WizardStep:
    slug: str
    title: str
    keys: tuple[str, ...]


# Each step is a curated handful of keys (not a whole catalog group) picked
# for what a fresh install actually needs to decide before going on air —
# advanced/rarely-touched fields from the same groups (e.g. BEACON_TICK_SECONDS,
# BEACON_TTS_PIPER_BINARY) stay editable later via /config, same as always.
SETUP_WIZARD_STEPS: tuple[WizardStep, ...] = (
    WizardStep("identity", "Beacon identity", REQUIRED_BEACON_KEYS),
    WizardStep("transmission", "Transmission", ("BEACON_ENABLED", "BEACON_TYPE")),
    WizardStep(
        "wav-handoff",
        "Radio hand-off",
        ("BEACON_WAV_TRANSMITTER", "BEACON_TXQUEUE_INCOMING_DIR"),
    ),
    WizardStep(
        "voice",
        "Voice",
        ("BEACON_TTS_ENGINE", "BEACON_TTS_VOICE", "BEACON_TTS_PIPER_MODEL"),
    ),
    WizardStep(
        "watermark",
        "On-air watermark",
        ("BEACON_WATERMARK_ENABLED", "BEACON_WATERMARK_INTERVAL_SECONDS"),
    ),
    WizardStep(
        "ai",
        "AI summarization",
        (
            "ACTIONS_AI_ENABLED",
            "ACTIONS_AI_PROVIDER",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "ACTIONS_AI_OLLAMA_HOST",
        ),
    ),
    WizardStep(
        "display",
        "Display & language",
        ("DISPLAY_TIMEZONE", "UI_DEFAULT_LOCALE", "UI_DEFAULT_THEME"),
    ),
    # Empty `keys` — unlike every other step, this isn't a curated
    # SettingSpec subset (a logo upload isn't a form field); see
    # routers/setup.py's `if step.slug == "branding"` branch, the one
    # place that special-cases this step's GET/POST instead of going
    # through the shared _build_fields/save_settings machinery. Placed
    # last (not, say, right after "identity") so inserting it doesn't
    # renumber/reorder any of the other steps' slugs.
    WizardStep("branding", "Branding", ()),
)

FINISH_SLUG = "finish"

_SLUG_TO_STEP: dict[str, WizardStep] = {step.slug: step for step in SETUP_WIZARD_STEPS}


def step_for_slug(slug: str) -> WizardStep | None:
    return _SLUG_TO_STEP.get(slug)


def next_step_slug(slug: str) -> str:
    """The wizard step after `slug`, or FINISH_SLUG after the last one."""
    index = [step.slug for step in SETUP_WIZARD_STEPS].index(slug)
    if index + 1 < len(SETUP_WIZARD_STEPS):
        return SETUP_WIZARD_STEPS[index + 1].slug
    return FINISH_SLUG


def prev_step_slug(slug: str) -> str | None:
    """The wizard step before `slug`, or None for the first step / finish's
    predecessor being the last real step."""
    if slug == FINISH_SLUG:
        return SETUP_WIZARD_STEPS[-1].slug
    index = [step.slug for step in SETUP_WIZARD_STEPS].index(slug)
    return SETUP_WIZARD_STEPS[index - 1].slug if index > 0 else None


def step_index(slug: str) -> int:
    """0-based position of `slug` among SETUP_WIZARD_STEPS, or
    len(SETUP_WIZARD_STEPS) for FINISH_SLUG — used by the wizard's progress
    indicator to mark earlier steps "done" without the template needing to
    compare WizardStep objects itself."""
    if slug == FINISH_SLUG:
        return len(SETUP_WIZARD_STEPS)
    return [step.slug for step in SETUP_WIZARD_STEPS].index(slug)
