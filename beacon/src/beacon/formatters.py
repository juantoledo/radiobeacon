"""Turns resolved content (content.py) into channel-ready text.

Length limits are sourced differently per channel — see __main__.py's
module docstring for the full reasoning. Frame content is deliberately NOT
a new beacon-specific setting — it reuses ACTIONS_CHUNK_MAX_CHARS (already
sized, per actions/chunk.py's own docstring, "to leave headroom under
AX.25's ~256-byte UI frame payload limit... for whatever a future
AX.25-formatter action adds" — beacon's frame content *is* that action).
Voice content has its own dedicated BEACON_VOICE_MAX_CHARS: items.summary
is now always populated (actions.ai copies extracted_contents in verbatim
when there's nothing to condense — see ai.py), so voice text can be
arbitrarily long raw content, not just a short AI summary — reusing
ACTIONS_AI_MAX_CHARS (whose real job is gating whether actions.ai's LLM
call even runs) would silently cap voice at whatever that skip-threshold
happens to be, coincidentally matching frame's own chunk width.

Frame rejects, voice truncates: an assembled TNC2 line over the AX.25
protocol limit raises FrameTooLongError rather than being silently cut —
this repo's SENAPRED-sourced content often puts the "fuente experimental
no oficial, consulte SENAPRED" disclaimer at the end, and truncating could
drop exactly that. Voice has no protocol-level limit — BEACON_VOICE_TEMPLATE
combined with a max_chars truncation is purely a time-budget cap, so a
word-boundary truncation (reusing actions.chunk's textwrap approach) is an
acceptable, much less destructive tradeoff there.

Both channels' prefix/suffix (BEACON_FRAME_PREFIX/SUFFIX,
BEACON_VOICE_PREFIX/SUFFIX) — and voice's outer BEACON_VOICE_TEMPLATE —
are str.format templates that, beyond {date}, can reference item_fields:
{source}, {item_id}, {type}, {subtype}, {extracted_title}, {url} (see
beacon.content.resolve_item_fields). Rendered via adapters.templating.
safe_format, which falls back to "" on an invalid placeholder rather than
raising — a misconfigured template must never crash the transmit loop,
which has no per-tick catch-all."""
import textwrap
from dataclasses import dataclass

from adapters.ax25 import AX25_HARD_LIMIT_BYTES as _AX25_HARD_LIMIT_BYTES
from adapters.templating import safe_format

# AX.25 UI frame payload limit is ~256 bytes (some implementations tolerate
# up to ~300-330, per CONTEXT.md) — this is a protocol-safety net, not a
# user-facing setting, checked against the FULL assembled line (including
# the CALLSIGN>DEST: prefix), not just the chunk text alone. Shared with
# actions.chunk (adapters.ax25.max_frame_content_bytes) so both sides of
# the frame-sizing story agree on one number.


class FrameTooLongError(ValueError):
    pass


@dataclass(frozen=True)
class FormattedFrame:
    tnc2: str
    content: str
    byte_length: int


@dataclass(frozen=True)
class FormattedVoice:
    text: str
    truncated: bool


def format_frame(
    chunk_text: str, *, callsign: str, destination: str, prefix: str = "", suffix: str = "", date: str = "",
    **item_fields: str,
) -> FormattedFrame:
    """prefix/suffix (BEACON_FRAME_PREFIX/BEACON_FRAME_SUFFIX) wrap the
    actual transmitted payload -- applied to every frame, including each
    chunk of a multi-frame item, not just once per item, so a listener
    catching only one frame still sees it. Distinct from `destination`
    (the AX.25 tocall address, e.g. NFO) -- this wraps the info field a
    listener actually decodes as content.

    prefix/suffix are str.format templates, not plain literals -- {date}
    plus whatever's passed as item_fields (see content.resolve_item_fields
    and this module's own docstring) are the supported placeholders. A
    literal string with none of those in it (e.g. today's
    " [EXPERIMENTAL]") passes through unchanged. Rendered via
    adapters.templating.safe_format -- an invalid placeholder logs an
    error and falls back to "" rather than raising."""
    prefix = safe_format(prefix, "BEACON_FRAME_PREFIX", date=date, **item_fields)
    suffix = safe_format(suffix, "BEACON_FRAME_SUFFIX", date=date, **item_fields)
    content = f"{prefix}{chunk_text}{suffix}"
    tnc2 = f"{callsign}>{destination}:{content}"
    byte_length = len(tnc2.encode("utf-8"))
    if byte_length > _AX25_HARD_LIMIT_BYTES:
        raise FrameTooLongError(
            f"assembled frame is {byte_length} bytes, exceeds the "
            f"{_AX25_HARD_LIMIT_BYTES}-byte AX.25 UI frame limit"
        )
    return FormattedFrame(tnc2=tnc2, content=content, byte_length=byte_length)


def format_voice(
    text: str, *, callsign: str, template: str, max_chars: int,
    prefix: str = "", suffix: str = "", date: str = "", **item_fields: str,
) -> FormattedVoice:
    """text is truncated to max_chars FIRST (word-boundary, unchanged
    behavior), THEN wrapped with prefix/suffix (BEACON_VOICE_PREFIX/SUFFIX)
    -- mirroring format_frame, where prefix/suffix wrap the
    already-sized chunk_text rather than counting against its budget.
    Voice has no hard protocol limit, so unlike frame this is never
    rejected, only ever a (slightly) longer time-budget cap in practice.

    template (BEACON_VOICE_TEMPLATE), and prefix/suffix, are str.format
    templates -- {callsign}, {text}, {date}, plus item_fields (see
    content.resolve_item_fields) are all available in every one of the
    three, via adapters.templating.safe_format (falls back to "" on an
    invalid placeholder rather than raising)."""
    truncated = False
    if len(text) > max_chars:
        pieces = textwrap.wrap(text, width=max_chars, break_long_words=False, break_on_hyphens=False)
        text = pieces[0] if pieces else text[:max_chars]
        truncated = True
    wrapped_text = (
        safe_format(prefix, "BEACON_VOICE_PREFIX", date=date, **item_fields)
        + text
        + safe_format(suffix, "BEACON_VOICE_SUFFIX", date=date, **item_fields)
    )
    rendered = safe_format(
        template, "BEACON_VOICE_TEMPLATE", callsign=callsign, text=wrapped_text, date=date, **item_fields
    )
    return FormattedVoice(text=rendered, truncated=truncated)
