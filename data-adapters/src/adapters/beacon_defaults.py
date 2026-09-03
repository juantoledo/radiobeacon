"""Shared default values for beacon's enable/type/queue settings and the
voice content templates — single source of truth for beacon.__main__ (owns
the transmit loop), ui.routers.beacon (needs the same numbers for the
dashboard's beacon panel), and config_catalog.py's own SettingSpec defaults
for these keys."""

BEACON_ENABLED_DEFAULT = "false"
BEACON_TYPE_DEFAULT = "voice"  # "voice" | "frame"
BEACON_QUEUE_MAX_SIZE_DEFAULT = "200"

# How long a beacon_tx_schedule row may sit unsent before the transmit loop
# drops it instead of putting it on air (see
# beacon.__main__._purge_stale_rows). Measured against the row's `updated_at`
# — the last time it was enqueued, rearmed, or transmitted — so a deliberate
# rearm / the ui's "Re-transmit" refreshes the clock. The point: turning
# transmit off overnight must not dump a stale backlog on air when it comes
# back. Checked every tick regardless of BEACON_ENABLED. "0" disables the
# check. Does not touch beacon_manual_tx (one-shot operator messages).
BEACON_MAX_QUEUED_AGE_SECONDS_DEFAULT = "21600"  # 6 hours

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

# Attention tone: a short sequence of beeps prepended to every rendered voice
# WAV (scheduled bulletins and manual "Transmit now" alike -- not the
# watermark) so listeners recognize an announcement is starting. See
# adapters.attention_tone. Comma-separated `freq:ms` pairs, freq in Hz with
# 0 meaning silence. The default is a ~2 s rising-then-falling seven-note
# sweep; set it blank to disable the tone entirely.
BEACON_VOICE_ATTENTION_TONE_DEFAULT = "500:250,600:250,700:300,800:300,700:250,600:250,500:350"

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

# Manual transmission: a one-shot message an operator types into the
# dashboard and sends immediately (see beacon.__main__._drain_manual_tx and
# ui.routers.manual_tx). It has no item and no transmit_policy — it's sent
# once, on the next tick, and dropped. This outer template wraps the typed
# text for the voice channel so the callsign is always spoken; {callsign}
# and {text} are the placeholders (rendered via adapters.templating.
# safe_format). The frame channel needs no template — the callsign is
# already in the AX.25 "{callsign}>{destination}:" header.
BEACON_MANUAL_VOICE_TEMPLATE_DEFAULT = "Aquí {callsign}. {text}"
