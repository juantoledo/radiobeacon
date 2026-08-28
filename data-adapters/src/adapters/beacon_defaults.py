"""Shared default values for beacon's TDMA window/enable/queue settings —
single source of truth for beacon.__main__ (owns the scheduling logic),
ui.routers.beacon (needs the same numbers for presentation math only:
the /beacon page's timeline/queue-bar rendering), and
config_catalog.py's own SettingSpec defaults for these keys."""

BEACON_ENABLED_DEFAULT = "false"
BEACON_WINDOW_TOTAL_SECONDS_DEFAULT = "90"
BEACON_WINDOW_VOICE_SECONDS_DEFAULT = "60"
BEACON_WINDOW_GUARD_SECONDS_DEFAULT = "0"
BEACON_WINDOW_FRAME_SECONDS_DEFAULT = "30"
BEACON_QUEUE_MAX_SIZE_DEFAULT = "200"
