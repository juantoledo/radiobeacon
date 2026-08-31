"""Shared default values for beacon's enable/type/queue settings — single
source of truth for beacon.__main__ (owns the transmit loop),
ui.routers.beacon (needs the same numbers for the dashboard's beacon panel),
and config_catalog.py's own SettingSpec defaults for these keys."""

BEACON_ENABLED_DEFAULT = "false"
BEACON_TYPE_DEFAULT = "voice"  # "voice" | "frame"
BEACON_QUEUE_MAX_SIZE_DEFAULT = "200"
