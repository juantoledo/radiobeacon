"""Per-adapter voice pronunciation replacements — a small key -> value map an
operator configures on each adapter (adapter_instances.config["voice_replacements"])
to expand abbreviations *in the spoken output only*, so TTS says "kilómetros"
instead of spelling "km" and "Senapred" instead of "S-E-N-A-P-R-E-D".

Applied by the beacon in _transmit_voice_unit, on the resolved item text
before formatters.format_voice truncates/wraps it — see
beacon.content.resolve_voice_replacements. The canonical items.summary and the
byte-starved AX.25 frame text are never touched.

SUGGESTED_VOICE_REPLACEMENTS is *only* a starting point the adapter form can
drop into the editor with one click; it is never applied on its own. An
adapter with no saved map applies nothing.

Matching semantics (apply_voice_replacements):
  * whole-word, CASE-SENSITIVE — only the abbreviation's own casing matches,
    the primary guard against Spanish-word collisions ("no", "se", "o").
  * longest key first — Python `re` alternation is first-alternative-wins,
    not longest-match, so "NNE" must be tried before "NE".
  * one compiled alternation, one left-to-right pass — an expansion is never
    itself re-scanned ("km" -> "kilómetros" can't re-match).
  * word-boundary guards are added only on a key's word-char edges, so
    punctuation-edged keys ("km/h", "N°", "aprox.", "FF.AA.") still match.
  * "5 km" matches but "5km" does not (a digit is a word char) — an operator
    adds an explicit row if a source runs numbers into units.
  * fail-soft: a map that won't compile logs and returns the text unchanged —
    a misconfigured adapter must never crash the transmit loop (same spirit
    as adapters.templating.safe_format).
"""
import logging
import re

logger = logging.getLogger(__name__)

# A *suggestion* the adapter form offers via a "Load suggested set" button —
# NEVER applied on its own. Deliberately no bare single-letter cardinals:
# "O"/"E" are Spanish words ("o" = or, "e" = and) and even case-sensitive
# matching is too risky for an on-air read.
SUGGESTED_VOICE_REPLACEMENTS: dict[str, str] = {
    # units — lowercase forms are never Spanish words
    "km": "kilómetros",
    "km/h": "kilómetros por hora",
    "m/s": "metros por segundo",
    "cm": "centímetros",
    "mm": "milímetros",
    "hrs": "horas",
    "aprox.": "aproximadamente",
    "Nº": "número",
    "N°": "número",
    # magnitude / time notation
    "Mw": "magnitud momento",
    "ML": "magnitud local",
    "UTC": "tiempo universal coordinado",
    # compass — "NO" (= noroeste) and "SE" (= sureste) are omitted on
    # purpose: they collide with the Spanish words "no" and "se", and
    # emergency copy is sometimes ALL CAPS, which defeats the case-sensitive
    # guard. Operators add them per adapter if a source needs them.
    "NE": "noreste",
    "SO": "suroeste",
    "NNE": "nornoreste",
    "ENE": "estenoreste",
    "ESE": "estesureste",
    "SSE": "sursureste",
    "SSO": "sursuroeste",
    "OSO": "oestesuroeste",
    "ONO": "oestenoroeste",
    "NNO": "nornoroeste",
    # Chilean emergency / seismic agencies — acronym-cased so TTS expands
    # them instead of spelling letter-by-letter
    "SENAPRED": "Senapred",
    "ONEMI": "Onemi",
    "CSN": "Centro Sismológico Nacional",
    "SHOA": "Servicio Hidrográfico y Oceanográfico de la Armada",
    "SNAM": "Sistema Nacional de Alarma de Maremotos",
    "DMC": "Dirección Meteorológica de Chile",
    "RM": "Región Metropolitana",
}


def _word_edge(char: str) -> bool:
    """Whether a boundary guard is meaningful next to `char` — true for the
    word characters `re`'s \\b cares about (alphanumerics + underscore)."""
    return char.isalnum() or char == "_"


def apply_voice_replacements(text: str, replacements: dict[str, str]) -> str:
    """Expand `replacements` in `text` — whole-word, case-sensitive, longest
    key first, single pass. Returns `text` unchanged when there is nothing to
    do or the assembled pattern won't compile. See the module docstring for
    the full semantics."""
    if not text or not replacements:
        return text
    keys = sorted(replacements, key=len, reverse=True)
    alternatives = []
    for key in keys:
        left = r"(?<!\w)" if _word_edge(key[0]) else ""
        right = r"(?!\w)" if _word_edge(key[-1]) else ""
        alternatives.append(f"{left}{re.escape(key)}{right}")
    try:
        pattern = re.compile("|".join(alternatives))
    except re.error:
        logger.exception("voice replacements did not compile; leaving text unchanged")
        return text
    return pattern.sub(lambda m: replacements[m.group(0)], text)
