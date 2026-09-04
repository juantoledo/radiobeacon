"""A minimal, independent copy of ui.config_catalog.SETTINGS_CATALOG's
{key: (type, choices)} — just enough for adapters.config_transfer (and
anything else in this shared layer or a CLI) to validate/coerce an
imported settings value, without depending on ui/ at all.

Why this duplicates rather than imports: ui/ and dispatcher/ both depend on
data-adapters/, never the other way around (ui.config_catalog says so
explicitly — "presentation metadata only ... never imported outside this
package"), so this package structurally cannot import SETTINGS_CATALOG.
Reversing that dependency, or hoisting SETTINGS_CATALOG's non-presentation
fields into this package, would be a real refactor of an 83-entry catalog
for the sake of one feature — out of scope here. This file is the smaller,
lower-risk fix: a hand-checked copy plus a cross-check test
(ui/tests/test_config_catalog.py::test_settings_registry_matches_catalog)
that fails the suite the moment the two drift, so staleness can't survive
unnoticed. If keeping them in sync ever becomes a recurring pain, that's
the signal to revisit the bigger refactor instead.

SECRET_SETTING_KEYS exists purely as defense in depth for
config_transfer.import_config: an export never writes a secret key in the
first place (skipped at the source, via `settings.is_secret`), but a
hand-edited or maliciously-crafted import file could still name one, and
this catches that independent of whatever `settings` row state happens to
say on the target DB."""

SECRET_SETTING_KEYS: frozenset[str] = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"})

# key -> (type, choices). `type` mirrors ui.config_catalog.SettingSpec.type
# ("text" | "int" | "float" | "bool" | "select"; "secret" entries are
# omitted entirely — see SECRET_SETTING_KEYS above). `choices` is only
# non-empty for type == "select". Generated from SETTINGS_CATALOG and
# checked against it by test_settings_registry_matches_catalog — do not
# hand-edit without also updating the catalog (or vice versa).
SETTINGS_REGISTRY: dict[str, tuple[str, tuple[str, ...]]] = {
    "ADAPTERS_DEFAULT_INTERVAL_SECONDS": ("int", ()),
    "DISPATCHER_INTERVAL_SECONDS": ("int", ()),
    "DISPATCHER_CONSUMER_NAME": ("text", ()),
    "DISPATCHER_MQ_HOST": ("text", ()),
    "DISPATCHER_MQ_PORT": ("int", ()),
    "DISPATCHER_MQ_QOS": ("int", ()),
    "DISPATCHER_MQ_CONNECT_TIMEOUT_SECONDS": ("int", ()),
    "ACTIONS_MQ_HOST": ("text", ()),
    "ACTIONS_MQ_PORT": ("int", ()),
    "ACTIONS_MQ_QOS": ("int", ()),
    "ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS": ("int", ()),
    "ACTIONS_CHUNK_SUBSCRIBE_TOPIC": ("text", ()),
    "ACTIONS_CHUNK_OUTPUT_TOPIC": ("text", ()),
    "ACTIONS_CHUNK_OUTPUT_EVENT_TYPE": ("text", ()),
    "ACTIONS_CHUNK_MAX_CHARS": ("int", ()),
    "ACTIONS_AI_ENABLED": ("bool", ()),
    "ACTIONS_AI_SUBSCRIBE_TOPIC": ("text", ()),
    "ACTIONS_AI_OUTPUT_TOPIC": ("text", ()),
    "ACTIONS_AI_OUTPUT_EVENT_TYPE": ("text", ()),
    "ACTIONS_AI_PROVIDER": ("select", ("openai", "claude", "ollama")),
    "ACTIONS_AI_CLAUDE_MODEL": ("text", ()),
    "ACTIONS_AI_OPENAI_MODEL": ("text", ()),
    "ACTIONS_AI_OLLAMA_MODEL": ("text", ()),
    "ACTIONS_AI_OLLAMA_HOST": ("text", ()),
    "ACTIONS_AI_MAX_CHARS": ("int", ()),
    "ACTIONS_AI_PROMPT": ("text", ()),
    "ACTIONS_AI_EVENT_INCLUDE_PROMPT": ("bool", ()),
    "ACTIONS_CONTENT_READY_POLL_INTERVAL_SECONDS": ("int", ()),
    "ACTIONS_CONTENT_READY_OUTPUT_TOPIC": ("text", ()),
    "DISPLAY_TIMEZONE": ("text", ()),
    "UI_PAGE_SIZE": ("int", ()),
    "UI_DEV_TOOLS_ENABLED": ("bool", ()),
    "UI_DASHBOARD_REFRESH_SECONDS": ("int", ()),
    "UI_DEFAULT_CONSUMER_NAME": ("text", ()),
    "BEACON_CALLSIGN": ("text", ()),
    "BEACON_DESCRIPTION": ("text", ()),
    "BEACON_SHORT_DESCRIPTION": ("text", ()),
    "BEACON_OPERATOR_CONTACT": ("text", ()),
    "BEACON_GRID_LOCATOR": ("text", ()),
    "BEACON_FREQUENCY": ("text", ()),
    "BEACON_ENABLED": ("bool", ()),
    "BEACON_TYPE": ("select", ("voice", "frame")),
    "BEACON_TICK_SECONDS": ("int", ()),
    "BEACON_INTER_TX_DELAY_SECONDS": ("float", ()),
    "BEACON_WAV_TRANSMITTER": ("select", ("logging", "spool")),
    "BEACON_TXQUEUE_INCOMING_DIR": ("text", ()),
    "BEACON_SVXLINK_CONF_PATH": ("text", ()),
    "BEACON_RF_CONF_EDITOR_ENABLED": ("bool", ()),
    "BEACON_VOICE_TEMPLATE": ("text", ()),
    "BEACON_VOICE_PREFIX": ("text", ()),
    "BEACON_VOICE_SUFFIX": ("text", ()),
    "BEACON_VOICE_MAX_CHARS": ("int", ()),
    "BEACON_DATE_FORMAT": ("text", ()),
    "BEACON_FRAME_DESTINATION": ("text", ()),
    "BEACON_FRAME_PREFIX": ("text", ()),
    "BEACON_FRAME_SUFFIX": ("text", ()),
    "BEACON_GEN_PACKETS_BINARY": ("text", ()),
    "BEACON_FRAME_LEAD_SILENCE_MS": ("int", ()),
    "BEACON_DIREWOLF_CONF_PATH": ("text", ()),
    "BEACON_TTS_ENGINE": ("select", ("piper", "espeak")),
    "BEACON_TTS_VOICE": ("text", ()),
    "BEACON_TTS_PIPER_MODEL": ("text", ()),
    "BEACON_TTS_PIPER_BINARY": ("text", ()),
    "BEACON_TTS_WAV_DIR": ("text", ()),
    "BEACON_MANUAL_VOICE_TEMPLATE": ("text", ()),
    "BEACON_VOICE_ATTENTION_TONE": ("text", ()),
    "BEACON_WATERMARK_ENABLED": ("bool", ()),
    "BEACON_WATERMARK_INTERVAL_SECONDS": ("int", ()),
    "BEACON_WATERMARK_VOICE_TEMPLATE": ("text", ()),
    "BEACON_WATERMARK_FRAME_TEMPLATE": ("text", ()),
    "BEACON_QUEUE_MAX_SIZE": ("int", ()),
    "BEACON_MAX_QUEUED_AGE_SECONDS": ("int", ()),
    "BEACON_CONTENT_READY_RECONCILE_INTERVAL_SECONDS": ("int", ()),
    "BEACON_MQ_HOST": ("text", ()),
    "BEACON_MQ_PORT": ("int", ()),
    "BEACON_MQ_QOS": ("int", ()),
    "BEACON_MQ_RECONNECT_BACKOFF_SECONDS": ("int", ()),
    "BEACON_CONTENT_READY_SUBSCRIBE_TOPIC": ("text", ()),
    "BEACON_NTP_SERVER": ("text", ()),
    "BEACON_NTP_CHECK_INTERVAL_SECONDS": ("int", ()),
    "BEACON_NTP_MAX_OFFSET_SECONDS": ("float", ()),
}
