"""Shared default values for beacon's enable/type/queue settings and the
voice content templates — single source of truth for beacon.__main__ (owns
the transmit loop), ui.routers.beacon (needs the same numbers for the
dashboard's beacon panel), and config_catalog.py's own SettingSpec defaults
for these keys."""

BEACON_ENABLED_DEFAULT = "false"
BEACON_TYPE_DEFAULT = "voice"  # "voice" | "frame"
BEACON_QUEUE_MAX_SIZE_DEFAULT = "200"

# Voice content templates (see beacon.formatters.format_voice). The outer
# template is a bare {text} passthrough — all the framing lives in the
# prefix/suffix, which wrap the resolved summary with a spoken-Spanish
# "informational bulletin" envelope naming the source and the item's own
# date. {source_name}/{date} plus the item-field placeholders
# ({type}, {subtype}, {extracted_title}, {url}, ...) are rendered via
# adapters.templating.safe_format (unknown placeholder -> ""). Prefix/suffix
# sit outside BEACON_VOICE_MAX_CHARS's truncation budget, so the envelope is
# always spoken in full even when the summary is cut.
BEACON_VOICE_TEMPLATE_DEFAULT = "{text}"
BEACON_VOICE_PREFIX_DEFAULT = "Información de {source_name}, {date}. "
BEACON_VOICE_SUFFIX_DEFAULT = (
    ". Para más información consulte fuentes oficiales de {source_name}. "
    "Fin del comunicado. "
)
# Word-boundary truncation budget for the resolved summary only (the
# prefix/suffix envelope above is always spoken in full on top of this).
BEACON_VOICE_MAX_CHARS_DEFAULT = "750"

# Watermark: a periodic, item-independent message on its own timer (see
# beacon.__main__._transmit_watermark), rendered through whichever
# BEACON_TYPE is currently active. Off by default -- an operator opts in
# and writes their own templates once they've set BEACON_CALLSIGN. Unlike
# the voice content templates above, these have no item behind them, so
# their placeholders are the beacon's own per-tick settings (callsign,
# date, destination, ...) rather than item fields -- see
# beacon.__main__._watermark_fields.
BEACON_WATERMARK_ENABLED_DEFAULT = "false"
BEACON_WATERMARK_INTERVAL_SECONDS_DEFAULT = "600"
BEACON_WATERMARK_VOICE_TEMPLATE_DEFAULT = "Estación {callsign}, transmisión automática. {date}."
BEACON_WATERMARK_FRAME_TEMPLATE_DEFAULT = "{callsign} watermark {date}"
