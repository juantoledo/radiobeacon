"""Turns resolved content (content.py) into channel-ready text.

Length limits are deliberately NOT new beacon-specific settings — see
__main__.py's module docstring for the full reasoning. Frame content
reuses ACTIONS_CHUNK_MAX_CHARS (already sized, per actions/chunk.py's own
docstring, "to leave headroom under AX.25's ~256-byte UI frame payload
limit... for whatever a future AX.25-formatter action adds" — beacon's
frame content *is* that action). Voice content reuses ACTIONS_AI_MAX_CHARS
as a defensive ceiling only (the AI prompt is the real length control;
this just guards against a misconfigured prompt producing runaway text).

Frame rejects, voice truncates: an assembled TNC2 line over the AX.25
protocol limit raises FrameTooLongError rather than being silently cut —
this repo's SENAPRED-sourced content often puts the "fuente experimental
no oficial, consulte SENAPRED" disclaimer at the end, and truncating could
drop exactly that. Voice has no protocol-level limit — BEACON_VOICE_TEMPLATE
combined with a max_chars truncation is purely a time-budget cap, so a
word-boundary truncation (reusing actions.chunk's textwrap approach) is an
acceptable, much less destructive tradeoff there."""
import textwrap
from dataclasses import dataclass

# AX.25 UI frame payload limit is ~256 bytes (some implementations tolerate
# up to ~300-330, per CONTEXT.md) — this is a protocol-safety net, not a
# user-facing setting, checked against the FULL assembled line (including
# the CALLSIGN>DEST: prefix), not just the chunk text alone.
_AX25_HARD_LIMIT_BYTES = 256


class FrameTooLongError(ValueError):
    pass


@dataclass(frozen=True)
class FormattedFrame:
    tnc2: str
    byte_length: int


@dataclass(frozen=True)
class FormattedVoice:
    text: str
    truncated: bool


def format_frame(chunk_text: str, *, callsign: str, destination: str) -> FormattedFrame:
    tnc2 = f"{callsign}>{destination}:{chunk_text}"
    byte_length = len(tnc2.encode("utf-8"))
    if byte_length > _AX25_HARD_LIMIT_BYTES:
        raise FrameTooLongError(
            f"assembled frame is {byte_length} bytes, exceeds the "
            f"{_AX25_HARD_LIMIT_BYTES}-byte AX.25 UI frame limit"
        )
    return FormattedFrame(tnc2=tnc2, byte_length=byte_length)


def format_voice(text: str, *, callsign: str, template: str, max_chars: int) -> FormattedVoice:
    truncated = False
    if len(text) > max_chars:
        pieces = textwrap.wrap(text, width=max_chars, break_long_words=False, break_on_hyphens=False)
        text = pieces[0] if pieces else text[:max_chars]
        truncated = True
    rendered = template.format(callsign=callsign, text=text)
    return FormattedVoice(text=rendered, truncated=truncated)
