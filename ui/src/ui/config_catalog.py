"""Static catalog of the env-var-backed settings the /config section exposes
for editing — presentation metadata only (labels, form widget type,
grouping), never imported outside this package. The actual read/write
happens through adapters.storage.get_setting/set_setting/list_settings,
keyed by the exact env var name each SettingSpec.key names.

Covers the adapters' shared default poll interval, actions (chunk, ai),
dispatcher, MQ infra, the two provider secrets, beacon (including
"Beacon — Identity", the operator profile — required, DB-only, see
SettingSpec.env_fallback/required below), and the "Display"/"UI" groups
(display timezone, page size, dev tools, refresh intervals, default
consumer name). Per-source adapter config (CSN, SENAPRED, and any
operator-added instance) lives in the adapter_instances table instead,
managed at /config/adapters — see ui.routers.adapters.
UI_HOST/UI_PORT/UI_DB_PATH are the only settings that stay env-only — they're
needed before the app can even reach its own database, so they can never be
DB-backed like everything else here (see ui.config).

A multi-group category's /config landing page groups its groups into labelled
Sections per CATEGORY_LAYOUT (e.g. Beacon's 11 groups become "Station",
"Content", "Radio hand-off", "Infrastructure") — presentation-only, layered on
top of the category/group split above; see ui.routers.config."""
from dataclasses import dataclass, field

from adapters.beacon_defaults import (
    BEACON_ENABLED_DEFAULT,
    BEACON_MANUAL_VOICE_TEMPLATE_DEFAULT,
    BEACON_MAX_QUEUED_AGE_SECONDS_DEFAULT,
    BEACON_QUEUE_MAX_SIZE_DEFAULT,
    BEACON_SVXLINK_LOG_PATH_DEFAULT,
    BEACON_TTS_RETENTION_DAYS_DEFAULT,
    BEACON_TX_MONITOR_ENABLED_DEFAULT,
    BEACON_TYPE_DEFAULT,
    BEACON_VOICE_ATTENTION_TONE_DEFAULT,
    BEACON_VOICE_MAX_CHARS_DEFAULT,
    BEACON_VOICE_PREFIX_DEFAULT,
    BEACON_VOICE_SUFFIX_DEFAULT,
    BEACON_VOICE_TEMPLATE_DEFAULT,
    BEACON_WATERMARK_ENABLED_DEFAULT,
    BEACON_WATERMARK_FRAME_TEMPLATE_DEFAULT,
    BEACON_WATERMARK_INTERVAL_SECONDS_DEFAULT,
    BEACON_WATERMARK_VOICE_TEMPLATE_DEFAULT,
)


@dataclass(frozen=True)
class SettingSpec:
    key: str
    group: str
    label: str
    description: str
    type: str  # "text" | "int" | "float" | "bool" | "select" | "secret"
    default: str | None
    is_secret: bool = False
    choices: tuple[str, ...] = field(default_factory=tuple)
    # wiring=True: changes pub/sub topology (topic/event-type wiring) —
    # a typo here silently breaks a pipeline with no visible error, unlike
    # a numeric threshold. Rendered with extra warning styling and a
    # confirm-before-save prompt in config_group_form.html. Always also
    # counts as advanced (see is_advanced) — wiring keys never belong on
    # the friendly Settings tab.
    wiring: bool = False
    # advanced=True: internal / rarely-changed plumbing (MQ host/port/QoS,
    # binary paths, poll & reconcile intervals, NTP) — shown on the
    # "Advanced" sub-tab of /config instead of the default "Settings" tab.
    # Presentation only; nothing about how the value is stored or read
    # changes. is_advanced(spec) is the effective test (advanced or wiring).
    advanced: bool = False
    # env_fallback=False: DB-only, never reads an identically-named env
    # var — used by beacon identity so a stray BEACON_CALLSIGN in the
    # process environment can never silently take effect (see
    # adapters.storage.get_setting's env_fallback parameter).
    env_fallback: bool = True
    # required=True: blank on submit rejects the WHOLE group save with a
    # 400 and an inline error, instead of the default "blank means no
    # change" (Reset is what clears an existing override). Used by
    # beacon identity, whose fields aren't meaningfully optional.
    required: bool = False


SETTINGS_CATALOG: list[SettingSpec] = [
    # --- Adapters — General ---
    SettingSpec(
        "ADAPTERS_DEFAULT_INTERVAL_SECONDS",
        "Adapters — General",
        "Default poll interval (s)",
        "Fallback fetch interval for a Policy whose fetch kind is "
        "'interval' with no interval set. The seeded 'default' Policy sets "
        "10s explicitly, so this only applies to hand-made Policies.",
        "int",
        "10",
    ),
    # Per-source adapter config (CSN, SENAPRED, and any operator-added
    # instance) no longer lives here — each is a row in the adapter_instances
    # table, managed at /config/adapters. See ui.routers.adapters.
    # --- Dispatcher ---
    SettingSpec(
        "DISPATCHER_INTERVAL_SECONDS",
        "Dispatcher",
        "Poll interval (s)",
        "How often dispatcher polls radiobeacon.db for new items to dispatch.",
        "int",
        "5",
    ),
    SettingSpec(
        "DISPATCHER_CONSUMER_NAME",
        "Dispatcher",
        "Consumer name",
        "Logical consumer name — its own row in dispatcher_state tracks its progress.",
        "text",
        "log",
        advanced=True,
    ),
    # --- MQ — Dispatcher ---
    SettingSpec(
        "DISPATCHER_MQ_HOST",
        "MQ — Dispatcher",
        "MQTT host",
        "Broker host for dispatcher's audit-event publishing. Defaults to "
        "localhost; set to an empty string to disable publishing entirely.",
        "text",
        "localhost",
        advanced=True,
    ),
    SettingSpec(
        "DISPATCHER_MQ_PORT",
        "MQ — Dispatcher",
        "MQTT port",
        "Broker port for dispatcher's audit-event publishing.",
        "int",
        "1883",
        advanced=True,
    ),
    SettingSpec(
        "DISPATCHER_MQ_QOS",
        "MQ — Dispatcher",
        "MQTT QoS",
        "QoS level for dispatcher's published events.",
        "int",
        "1",
        advanced=True,
    ),
    SettingSpec(
        "DISPATCHER_MQ_CONNECT_TIMEOUT_SECONDS",
        "MQ — Dispatcher",
        "Connect timeout (s)",
        "Timeout for dispatcher's MQTT connect/publish.",
        "int",
        "5",
        advanced=True,
    ),
    # --- MQ — Actions ---
    SettingSpec(
        "ACTIONS_MQ_HOST",
        "MQ — Actions",
        "MQTT host",
        "Broker host actions subscribe to and publish on.",
        "text",
        "localhost",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_MQ_PORT",
        "MQ — Actions",
        "MQTT port",
        "Broker port for actions.",
        "int",
        "1883",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_MQ_QOS",
        "MQ — Actions",
        "MQTT QoS",
        "QoS level for actions' subscribe/publish.",
        "int",
        "1",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS",
        "MQ — Actions",
        "Reconnect backoff (s)",
        "Seconds between actions' MQTT reconnect attempts.",
        "int",
        "5",
        advanced=True,
    ),
    # --- Actions — Chunk ---
    SettingSpec(
        "ACTIONS_CHUNK_SUBSCRIBE_TOPIC",
        "Actions — Chunk",
        "Subscribe topic",
        "MQTT topic ChunkAction subscribes to — actions.ai's own output, "
        "not item.dispatched directly, so chunk always runs after ai has "
        "settled. ChunkAction falls back to this exact value in code, so "
        "chunking works with no config; override only to re-wire the "
        "pipeline, or set empty to disable the action.",
        "text",
        "radiobeacon/events/item.ai_settled",
        wiring=True,
    ),
    SettingSpec(
        "ACTIONS_CHUNK_OUTPUT_TOPIC",
        "Actions — Chunk",
        "Output topic",
        "MQTT topic ChunkAction publishes item.chunked events to.",
        "text",
        "radiobeacon/events/item.chunked",
        wiring=True,
    ),
    SettingSpec(
        "ACTIONS_CHUNK_OUTPUT_EVENT_TYPE",
        "Actions — Chunk",
        "Output event type",
        "CloudEvent `type` string for ChunkAction's output.",
        "text",
        "item.chunked",
        wiring=True,
    ),
    SettingSpec(
        "ACTIONS_CHUNK_MAX_CHARS",
        "Actions — Chunk",
        "Max chars per chunk",
        "Max characters per chunk (word-boundary-safe) — a ceiling, not a "
        "fixed size: dynamically clamped down further at runtime if the "
        "current beacon callsign/destination/prefix/suffix would "
        "otherwise risk an assembled AX.25 frame exceeding its ~256-byte "
        "limit. Applies live — no restart needed.",
        "int",
        "200",
    ),
    # --- Actions — AI ---
    SettingSpec(
        "ACTIONS_AI_ENABLED",
        "Actions — AI",
        "Enabled",
        "Enables/disables AiAction entirely. Applies live — no restart needed.",
        "bool",
        "false",
    ),
    SettingSpec(
        "ACTIONS_AI_SUBSCRIBE_TOPIC",
        "Actions — AI",
        "Subscribe topic",
        "MQTT topic AiAction subscribes to. AiAction falls back to this "
        "exact value in code, so the pipeline runs with no config; "
        "override only to re-wire it, or set empty to disable the action.",
        "text",
        "radiobeacon/events/item.dispatched",
        wiring=True,
    ),
    SettingSpec(
        "ACTIONS_AI_OUTPUT_TOPIC",
        "Actions — AI",
        "Output topic",
        "MQTT topic AiAction publishes to — ALWAYS, once it has a valid "
        "source/item_id, even when it decided there's nothing to "
        "summarize (a summarized:false marker) — actions.chunk "
        "subscribes here so it always runs after ai has settled.",
        "text",
        "radiobeacon/events/item.ai_settled",
        wiring=True,
    ),
    SettingSpec(
        "ACTIONS_AI_OUTPUT_EVENT_TYPE",
        "Actions — AI",
        "Output event type",
        "CloudEvent `type` string for AiAction's output.",
        "text",
        "item.ai_settled",
        wiring=True,
    ),
    SettingSpec(
        "ACTIONS_AI_PROVIDER",
        "Actions — AI",
        "Provider",
        "Which LLM provider to call. Required once AI is enabled.",
        "select",
        None,
        choices=("openai", "claude", "ollama"),
    ),
    SettingSpec(
        "ACTIONS_AI_CLAUDE_MODEL",
        "Actions — AI",
        "Claude model",
        "Model name used when provider=claude.",
        "text",
        "claude-haiku-4-5",
    ),
    SettingSpec(
        "ACTIONS_AI_OPENAI_MODEL",
        "Actions — AI",
        "OpenAI model",
        "Model name used when provider=openai.",
        "text",
        "gpt-4o-mini",
    ),
    SettingSpec(
        "ACTIONS_AI_OLLAMA_MODEL",
        "Actions — AI",
        "Ollama model",
        "Model name used when provider=ollama.",
        "text",
        "llama3.2:1b",
    ),
    SettingSpec(
        "ACTIONS_AI_OLLAMA_HOST",
        "Actions — AI",
        "Ollama host",
        "Ollama server host URL, used when provider=ollama.",
        "text",
        "http://localhost:11434",
    ),
    SettingSpec(
        "ACTIONS_AI_MAX_CHARS",
        "Actions — AI",
        "Max input chars",
        "Skip-summarization threshold on input length — items at or under this "
        "length aren't summarized (extracted_contents is still copied into "
        "items.summary verbatim). Matches ACTIONS_CHUNK_MAX_CHARS's default "
        "exactly. Not used for voice length — see BEACON_VOICE_MAX_CHARS.",
        "int",
        "200",
    ),
    SettingSpec(
        "ACTIONS_AI_PROMPT",
        "Actions — AI",
        "Prompt template",
        "Global str.format-style override of the default summarization prompt. Any "
        "of an item's mapped adapter attributes can be referenced as a placeholder: "
        "{source}, {item_id}, {extracted_title}, {extracted_contents}, {summary}, {url}, "
        "{event_key}, {type}, {subtype}, {policy}, {source_date_time}, "
        "{fetched_at}, {captured_at}, {rawdata}, plus the source's display name and "
        "site URL as {source_name} / {source_url} — an unknown placeholder just renders "
        "blank. A single adapter can override this further via its own 'AI prompt "
        "override' field on the /config/adapters form.",
        "text",
        None,
    ),
    SettingSpec(
        "ACTIONS_AI_EVENT_INCLUDE_PROMPT",
        "Actions — AI",
        "Include prompt in events",
        "When on, the fully rendered prompt sent to the provider is attached to "
        "each item.ai_settled event and its action.ai.executed audit row (visible "
        "on /audit). Off by default — the prompt embeds the item's full text, so "
        "it bloats every event, log line, and audit row. The event/audit row "
        "already carry a `reason` (on skips) and `provider`/`model` (on success) "
        "regardless of this setting.",
        "bool",
        "false",
        advanced=True,
    ),
    # --- Actions — Content Ready ---
    SettingSpec(
        "ACTIONS_CONTENT_READY_POLL_INTERVAL_SECONDS",
        "Actions — Content Ready",
        "Poll interval (s)",
        "How often the content_ready watcher scans for items where both "
        "chunk and AI have finished, to publish item.content_ready.",
        "int",
        "2",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_CONTENT_READY_OUTPUT_TOPIC",
        "Actions — Content Ready",
        "Output topic",
        "MQTT topic item.content_ready events are published to — the "
        "single trigger beacon subscribes to for both voice and frame.",
        "text",
        "radiobeacon/events/item.content_ready",
        wiring=True,
    ),
    # --- Secrets ---
    SettingSpec(
        "ANTHROPIC_API_KEY",
        "Secrets",
        "Anthropic API key",
        "Used by AiAction when ACTIONS_AI_PROVIDER=claude. Never displayed once "
        "saved — leave unchanged to keep the current value.",
        "secret",
        None,
        is_secret=True,
    ),
    SettingSpec(
        "OPENAI_API_KEY",
        "Secrets",
        "OpenAI API key",
        "Used by AiAction when ACTIONS_AI_PROVIDER=openai. Never displayed once "
        "saved — leave unchanged to keep the current value.",
        "secret",
        None,
        is_secret=True,
    ),
    # --- Display ---
    SettingSpec(
        "DISPLAY_TIMEZONE",
        "Display",
        "Display timezone",
        "IANA timezone used to convert stored UTC datetimes for presentation "
        "only (UI pages, beacon's transmitted {date} placeholder) — never "
        "affects storage, which stays UTC. Applies live — no restart needed.",
        "text",
        "America/Santiago",
    ),
    # --- UI ---
    SettingSpec(
        "UI_PAGE_SIZE",
        "UI",
        "Page size",
        "Rows per page on the items list, audit log, and Developers item browser.",
        "int",
        "50",
    ),
    SettingSpec(
        "UI_DEV_TOOLS_ENABLED",
        "UI",
        "Developer tools enabled",
        "Enables the /dev section (raw item add/edit/delete, dispatch-state "
        "reset, a read-only SQL runner) — a larger attack surface than the "
        "rest of this auth-less app. False 404s every /dev/* route and hides "
        "its nav link. Applies live — no restart needed.",
        "bool",
        "true",
    ),
    SettingSpec(
        "UI_DASHBOARD_REFRESH_SECONDS",
        "UI",
        "Dashboard auto-refresh interval (s)",
        "How often the dashboard's browser-side auto-refresh re-polls. 0 disables it.",
        "int",
        "5",
    ),
    SettingSpec(
        "UI_DASHBOARD_TX_STREAM_URL",
        "UI",
        "Dashboard transmission audio stream",
        "HTTP(S) URL of a live audio stream carrying SvxLink's transmit "
        "audio — e.g. an ffmpeg/Icecast capture of an ALSA loopback or a "
        "PulseAudio monitor (see documentation/svxlink-txqueue-SETUP.md). "
        "When set, every open dashboard plays it while the rig is keyed "
        "(ON AIR monitor), with a per-viewer mute toggle. The UI proxies "
        "and fans it out, so bind the stream to localhost. Blank disables "
        "it. Applies live — no restart needed.",
        "text",
        "",
    ),
    SettingSpec(
        "UI_DEFAULT_CONSUMER_NAME",
        "UI",
        "Default consumer name",
        "Consumer name prefilled on the item detail/edit rearm form. When "
        "unset, falls back to the current effective DISPATCHER_CONSUMER_NAME.",
        "text",
        "log",
        advanced=True,
    ),
    SettingSpec(
        "UI_DEFAULT_LOCALE",
        "UI",
        "Default language",
        "Fallback UI language when a visitor's browser doesn't send a "
        "recognized Accept-Language and they haven't chosen one yet. A "
        "visitor's own choice (the EN/ES toggle) always overrides this, "
        "stored in a per-browser cookie. Applies live — no restart needed.",
        "select",
        "en",
        choices=("en", "es"),
    ),
    SettingSpec(
        "UI_DEFAULT_THEME",
        "UI",
        "Default theme",
        "Fallback color theme for a visitor who hasn't chosen one yet. A "
        "visitor's own choice (the System/Light/Dark toggle) always overrides "
        "this, stored in a per-browser cookie. \"System\" follows the "
        "visitor's OS/browser preference. Applies live — no restart needed.",
        "select",
        "system",
        choices=("system", "light", "dark"),
    ),
    # --- Logging ---
    SettingSpec(
        "LOG_LEVEL",
        "Logging",
        "Log level",
        "Minimum severity every service writes to its log. Resolves DB row "
        "-> LOG_LEVEL env var -> INFO, same as every other setting. The "
        "running services re-read this once per loop tick and the UI within "
        "~30s, so a change applies live — no restart needed. DEBUG also "
        "un-mutes the HTTP/MQTT client libraries.",
        "select",
        "INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    ),
    # --- Beacon — Identity ---
    # The operator profile a listener actually hears/reads — required,
    # DB-only (env_fallback=False: a stray BEACON_CALLSIGN env var must
    # never silently take effect), never falls back to a default since
    # there isn't a sensible one. is_beacon_configured() (ui/beacon.py)
    # sources its required-key list from this group.
    SettingSpec(
        "BEACON_CALLSIGN",
        "Beacon — Identity",
        "Callsign",
        "The beacon's amateur radio callsign.",
        "text",
        None,
        env_fallback=False,
        required=True,
    ),
    SettingSpec(
        "BEACON_DESCRIPTION",
        "Beacon — Identity",
        "Description",
        "Longer description of the beacon/project.",
        "text",
        None,
        env_fallback=False,
        required=True,
    ),
    SettingSpec(
        "BEACON_SHORT_DESCRIPTION",
        "Beacon — Identity",
        "Short description",
        "One-line summary, e.g. for compact displays.",
        "text",
        None,
        env_fallback=False,
        required=True,
    ),
    SettingSpec(
        "BEACON_OPERATOR_CONTACT",
        "Beacon — Identity",
        "Operator contact",
        "How to reach the operator (email, etc.).",
        "text",
        None,
        env_fallback=False,
        required=True,
    ),
    SettingSpec(
        "BEACON_GRID_LOCATOR",
        "Beacon — Identity",
        "Grid locator (QTH)",
        "Maidenhead grid square, e.g. FF46vb.",
        "text",
        None,
        env_fallback=False,
        required=True,
    ),
    SettingSpec(
        "BEACON_FREQUENCY",
        "Beacon — Identity",
        "Frequency",
        "The beacon's transmit frequency.",
        "text",
        None,
        env_fallback=False,
        required=True,
    ),
    # --- Beacon — Transmission ---
    # BEACON_ENABLED also gets a first-class Enable/Disable control on
    # the dashboard (see ui/src/ui/routers/dashboard.py) — it stays
    # listed here too for discoverability/consistency with every other
    # setting.
    SettingSpec(
        "BEACON_ENABLED",
        "Beacon — Transmission",
        "Enabled",
        "Whether the beacon transmit loop actually puts scheduled content on "
        "air. Re-read every tick — no restart needed to flip it.",
        "bool",
        BEACON_ENABLED_DEFAULT,
    ),
    SettingSpec(
        "BEACON_TYPE",
        "Beacon — Transmission",
        "Beacon type",
        "What each ready item is transmitted as. \"voice\" synthesizes a "
        "spoken-word WAV (piper/espeak). \"frame\" renders an AX.25 UI frame "
        "to a 1200-baud AFSK WAV via Direwolf's gen_packets, one per chunk. "
        "Either way a single WAV is produced and handed to the WAV "
        "transmitter (see Beacon — SvxLink). Changing this clears any "
        "pending rows of the other type on the next tick.",
        "select",
        BEACON_TYPE_DEFAULT,
        choices=("voice", "frame"),
    ),
    SettingSpec(
        "BEACON_TICK_SECONDS",
        "Beacon — Transmission",
        "Poll interval (s)",
        "Upper bound on how long the transmit loop waits before re-checking "
        "the schedule — newly queued content wakes it immediately, this is "
        "just the fallback poll interval.",
        "int",
        "2",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_INTER_TX_DELAY_SECONDS",
        "Beacon — Transmission",
        "Inter-transmission delay (s)",
        "Gap between consecutive transmissions when draining a backlog, so "
        "PTT / the svxlink-txqueue channel-idle wait can settle between "
        "clips.",
        "float",
        "2",
        advanced=True,
    ),
    # --- Beacon — SvxLink ---
    # How the rendered WAV is handed to SvxLink, plus an optional
    # in-dashboard editor for the real /etc/svxlink/svxlink.conf (off by
    # default — see BEACON_RF_CONF_EDITOR_ENABLED and ui.routers.rf_conf).
    SettingSpec(
        "BEACON_WAV_TRANSMITTER",
        "Beacon — SvxLink",
        "WAV transmitter",
        "\"logging\" (default, safe) just logs the WAV it would hand off. "
        "\"spool\" drops the WAV into the svxlink-txqueue spool for SvxLink "
        "to play when the RF channel is idle — see "
        "documentation/svxlink-txqueue-SETUP.md.",
        "select",
        "logging",
        choices=("logging", "spool"),
    ),
    SettingSpec(
        "BEACON_TXQUEUE_INCOMING_DIR",
        "Beacon — SvxLink",
        "svxlink-txqueue incoming dir",
        "Folder the \"spool\" WAV transmitter drops rendered WAV files into — "
        "svxlink-txqueue's documented drop point, which stamps a FIFO "
        "timestamp, validates, and sanitizes the name. Must match "
        "svxlink-txqueue's TXQUEUE_SPOOL/incoming.",
        "text",
        "/var/spool/svxlink-tx/incoming",
    ),
    SettingSpec(
        "BEACON_SVXLINK_CONF_PATH",
        "Beacon — SvxLink",
        "svxlink.conf path",
        "Absolute path to svxlink.conf, used ONLY by the in-dashboard editor "
        "on this page — the beacon never reads this file itself. Blank "
        "disables the editor. In the Docker deployment this file must also be "
        "bind-mounted into the UI container and writable by its user.",
        "text",
        "/etc/svxlink/svxlink.conf",
    ),
    SettingSpec(
        "BEACON_RF_CONF_EDITOR_ENABLED",
        "Beacon — SvxLink",
        "Enable svxlink.conf / direwolf.conf editor",
        "Allow editing svxlink.conf (this page) and direwolf.conf (the "
        "Direwolf page) from this dashboard. OFF by default: the dashboard "
        "has no login, so with this on, anyone who can reach it can read or "
        "overwrite any file the UI process can write. Only enable on a "
        "trusted host. A timestamped .bak is written on every save; no "
        "service is restarted (the page shows the systemctl command).",
        "bool",
        "false",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_TX_MONITOR_ENABLED",
        "Beacon — SvxLink",
        "Live ON AIR indicator",
        "Let the beacon process tail the SvxLink log for transmitter "
        "key-up / key-down and show a live \"ON AIR\" glow on the "
        "dashboard while a transmission is actually on the air. Read-only "
        "telemetry — it never touches SvxLink or transmission. "
        "Self-disabling: if the log below isn't present or readable "
        "(SvxLink on another host, in a container, or not set up yet) the "
        "indicator simply never appears.",
        "bool",
        BEACON_TX_MONITOR_ENABLED_DEFAULT,
    ),
    SettingSpec(
        "BEACON_SVXLINK_LOG_PATH",
        "Beacon — SvxLink",
        "SvxLink log path",
        "Log file the ON AIR indicator tails for "
        "\"Turning the transmitter ON/OFF\" (the same line svxlink-txqueue "
        "watches). The beacon process needs read access — typically by "
        "adding its user to the \"svxlink\" or \"adm\" group. In the Docker "
        "deployment the file must be bind-mounted into whichever container "
        "runs the beacon. Blank disables the indicator.",
        "text",
        BEACON_SVXLINK_LOG_PATH_DEFAULT,
        advanced=True,
    ),
    # --- Beacon — Templates ---
    SettingSpec(
        "BEACON_VOICE_TEMPLATE",
        "Beacon — Templates",
        "Voice template",
        "str.format-style template wrapping resolved voice content "
        "(itself already wrapped by BEACON_VOICE_PREFIX/SUFFIX below). "
        "Placeholders: {callsign}, {text}, {date} (blank if the item has "
        "no source_date_time), plus item fields {source}, {item_id}, "
        "{type}, {subtype}, {extracted_title}, {url}. Frame content has "
        "no template — its ORIGEN>DESTINO: structure is fixed protocol "
        "code — but BEACON_FRAME_PREFIX/SUFFIX support the same "
        "placeholders. An invalid placeholder falls back to \"\" rather "
        "than crashing the transmit loop. Defaults to a bare {text} "
        "passthrough — the framing lives in the prefix/suffix below.",
        "text",
        BEACON_VOICE_TEMPLATE_DEFAULT,
    ),
    SettingSpec(
        "BEACON_VOICE_PREFIX",
        "Beacon — Templates",
        "Voice content prefix",
        "Prepended to the resolved voice text before it's substituted "
        "into BEACON_VOICE_TEMPLATE's {text} — separate from "
        "BEACON_FRAME_PREFIX, which wraps frame content instead. A "
        "str.format template — {date} and the item-field placeholders "
        "(see BEACON_VOICE_TEMPLATE) are available. Added outside "
        "BEACON_VOICE_MAX_CHARS's truncation budget, mirroring how frame "
        "prefix/suffix wrap an already-sized chunk. Defaults to a "
        "spoken-Spanish bulletin opener naming the source and the item's date.",
        "text",
        BEACON_VOICE_PREFIX_DEFAULT,
    ),
    SettingSpec(
        "BEACON_VOICE_SUFFIX",
        "Beacon — Templates",
        "Voice content suffix",
        "Appended to the resolved voice text before it's substituted "
        "into BEACON_VOICE_TEMPLATE's {text}. Same placeholders and "
        "truncation-budget behavior as BEACON_VOICE_PREFIX. Defaults to a "
        "closing line pointing listeners to the source's official channels.",
        "text",
        BEACON_VOICE_SUFFIX_DEFAULT,
    ),
    SettingSpec(
        "BEACON_VOICE_MAX_CHARS",
        "Beacon — Templates",
        "Voice max chars",
        "Max characters of resolved voice text before word-boundary "
        "truncation (first piece only, no part markers — unlike frame "
        "chunking, the rest is silently dropped). A time-budget cap, not a "
        "protocol limit like frame's. Deliberately separate from "
        "ACTIONS_AI_MAX_CHARS, whose job is gating whether the LLM runs at all.",
        "int",
        BEACON_VOICE_MAX_CHARS_DEFAULT,
    ),
    SettingSpec(
        "BEACON_DATE_FORMAT",
        "Beacon — Templates",
        "Date format",
        "strftime format for {date} above — items.source_date_time "
        "converted to DISPLAY_TIMEZONE. Stick to fixed-width numeric "
        "directives (%d/%m/%Y/%H/%M); a weekday/month name (%A/%B) isn't "
        "accounted for by actions.chunk's AX.25 overflow-safety clamp.",
        "text",
        "%d-%m-%Y %H:%M",
    ),
    # --- Beacon — AX.25 ---
    SettingSpec(
        "BEACON_FRAME_DESTINATION",
        "Beacon — AX.25",
        "Frame destination (tocall)",
        "The AX.25 destination address — a software/project identifier, not "
        "a real route. NFO marks this as experimental, non-APRS traffic.",
        "text",
        "NFO",
    ),
    SettingSpec(
        "BEACON_FRAME_PREFIX",
        "Beacon — AX.25",
        "Frame content prefix",
        "Prepended to the actual transmitted payload (not the tocall "
        "address above) — applied to every frame, including each chunk of "
        "a multi-frame item. A str.format template — {date} plus item "
        "fields {source}, {item_id}, {type}, {subtype}, {extracted_title}, "
        "{url} are available (see Beacon — Templates). actions.chunk's "
        "dynamic max-chars clamp accounts for this item's real rendered "
        "values, not just the raw template.",
        "text",
        "",
    ),
    SettingSpec(
        "BEACON_FRAME_SUFFIX",
        "Beacon — AX.25",
        "Frame content suffix",
        "Appended to the actual transmitted payload — e.g. \"[EXPERIMENTAL]\" "
        "or \" {date}\". Applied to every frame, including each chunk of a "
        "multi-frame item, so a listener catching only one still sees it. "
        "Counts against the same 256-byte AX.25 hard limit as the rest of "
        "the frame — actions.chunk's dynamic max-chars clamp accounts for "
        "this item's real rendered values (same placeholders as "
        "BEACON_FRAME_PREFIX), not just this template's raw length.",
        "text",
        "",
    ),
    # --- Beacon — Direwolf ---
    # Frame rendering via Direwolf's one-shot `gen_packets` CLI (no Direwolf
    # process runs), plus an optional editor for a real direwolf.conf if you
    # run a full Direwolf instance on this host (off by default — shares
    # BEACON_RF_CONF_EDITOR_ENABLED with the SvxLink page).
    SettingSpec(
        "BEACON_GEN_PACKETS_BINARY",
        "Beacon — Direwolf",
        "gen_packets binary",
        "Command used to render an AX.25 frame to an AFSK WAV when "
        "BEACON_TYPE=frame. Ships with the `direwolf` package "
        "(apt install direwolf) — no Direwolf process runs, it's invoked "
        "one-shot per frame.",
        "text",
        "gen_packets",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_FRAME_LEAD_SILENCE_MS",
        "Beacon — Direwolf",
        "Frame lead silence (ms)",
        "Milliseconds of silence prepended to each rendered frame WAV so the "
        "first bits aren't clipped while SvxLink keys the transmitter.",
        "int",
        "250",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_DIREWOLF_CONF_PATH",
        "Beacon — Direwolf",
        "direwolf.conf path",
        "Absolute path to a direwolf.conf, if you run a full Direwolf "
        "instance on this host. radiobeacon itself only uses `gen_packets` "
        "and needs no direwolf.conf, so this is blank by default — blank "
        "hides the editor. Requires BEACON_RF_CONF_EDITOR_ENABLED (on the "
        "SvxLink page) to be on.",
        "text",
        "",
    ),
    # --- Beacon — Voice ---
    SettingSpec(
        "BEACON_TTS_ENGINE",
        "Beacon — Voice",
        "TTS engine",
        "\"piper\" (default) is offline neural TTS — much more natural, "
        "using the voice model shipped at BEACON_TTS_PIPER_MODEL. "
        "\"espeak\" is an offline/zero-setup fallback but sounds robotic.",
        "select",
        "piper",
        choices=("piper", "espeak"),
    ),
    SettingSpec(
        "BEACON_TTS_VOICE",
        "Beacon — Voice",
        "TTS voice (espeak)",
        "espeak-ng voice/language code used to synthesize speech. Only "
        "applies when BEACON_TTS_ENGINE=espeak.",
        "text",
        "es",
    ),
    SettingSpec(
        "BEACON_TTS_PIPER_MODEL",
        "Beacon — Voice",
        "Piper voice model path",
        "Path to a downloaded piper .onnx voice model (its .onnx.json "
        "sidecar must sit alongside it). Required when "
        "BEACON_TTS_ENGINE=piper. Defaults to the es_MX (Latin American "
        "Spanish) voice model shipped at beacon/storage/piper_voices/.",
        "text",
        "storage/piper_voices/es_MX-claude-high.onnx",
    ),
    SettingSpec(
        "BEACON_TTS_PIPER_BINARY",
        "Beacon — Voice",
        "Piper binary",
        "Command used to invoke piper. Only applies when "
        "BEACON_TTS_ENGINE=piper.",
        "text",
        "piper",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_TTS_WAV_DIR",
        "Beacon — Voice",
        "TTS output directory",
        "Where the beacon writes synthesized WAV files before transmitting. "
        "A relative path is resolved against the repo root (not beacon/); the "
        "dashboard reads this same directory to play a bulletin's voice clip "
        "back in the browser, so in a split host/container deployment it must "
        "point at storage shared by both.",
        "text",
        "storage/beacon_tts",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_TTS_RETENTION_DAYS",
        "Beacon — Voice",
        "Clip retention (days)",
        "How many days a rendered WAV clip is kept in the TTS output "
        "directory before the beacon prunes it (checked about hourly). Every "
        "airing keeps its own clip so it can be played back from the "
        "dashboard and the item page; 0 disables pruning (files accumulate "
        "forever).",
        "int",
        BEACON_TTS_RETENTION_DAYS_DEFAULT,
        advanced=True,
    ),
    SettingSpec(
        "BEACON_MANUAL_VOICE_TEMPLATE",
        "Beacon — Voice",
        "Manual message template",
        "Wraps text typed into the dashboard's \"Transmit now\" action for "
        "the voice channel, so the callsign is always spoken. Placeholders: "
        "{callsign}, {text} (an invalid one falls back to \"\"). The frame "
        "channel needs no template — the callsign is already in the AX.25 "
        "header.",
        "text",
        BEACON_MANUAL_VOICE_TEMPLATE_DEFAULT,
        advanced=True,
    ),
    SettingSpec(
        "BEACON_VOICE_ATTENTION_TONE",
        "Beacon — Voice",
        "Attention tone",
        "A short sequence of beeps prepended to every rendered voice WAV "
        "(scheduled bulletins and \"Transmit now\" alike — not the watermark) "
        "so listeners know an announcement is starting. Comma-separated "
        "\"freq:ms\" pairs, freq in Hz with 0 meaning silence — e.g. "
        "\"1400:250,0:120,1400:250\" is two 250 ms tones 120 ms apart. "
        "Defaults to a short rising/falling sweep; clear the field for no "
        "tone. Use the Preview button to hear it before saving.",
        "text",
        BEACON_VOICE_ATTENTION_TONE_DEFAULT,
    ),
    # --- Beacon — Watermark ---
    # A periodic, item-independent message on its own timer -- see
    # beacon.__main__._transmit_watermark. Off by default. Its templates
    # have no item behind them, so their placeholders are the beacon's own
    # settings (this whole page, effectively) rather than item fields:
    # {callsign}, {destination}, {date}, {voice_template}, {voice_prefix},
    # {voice_suffix}, {voice_max_chars}, {frame_prefix}, {frame_suffix},
    # {tts_voice}, {tts_engine}, {tts_piper_model}, {tts_piper_binary},
    # {gen_packets_binary}, {frame_lead_silence_ms}, {wav_dir},
    # {date_format} -- an invalid placeholder falls back to "" rather than
    # crashing the transmit loop.
    SettingSpec(
        "BEACON_WATERMARK_ENABLED",
        "Beacon — Watermark",
        "Enabled",
        "Whether the periodic watermark message transmits on its own timer, "
        "independent of item content. Re-read every tick.",
        "bool",
        BEACON_WATERMARK_ENABLED_DEFAULT,
    ),
    SettingSpec(
        "BEACON_WATERMARK_INTERVAL_SECONDS",
        "Beacon — Watermark",
        "Interval (s)",
        "How often the watermark transmits. Tracked independently of item "
        "transmissions and of BEACON_TICK_SECONDS.",
        "int",
        BEACON_WATERMARK_INTERVAL_SECONDS_DEFAULT,
    ),
    SettingSpec(
        "BEACON_WATERMARK_VOICE_TEMPLATE",
        "Beacon — Watermark",
        "Voice template",
        "str.format-style template for the watermark message when "
        "BEACON_TYPE=voice. Placeholders: {callsign}, {date}, plus every "
        "other beacon attribute (see this group's description above). "
        "Rendered through the same TTS engine/voice as regular content.",
        "text",
        BEACON_WATERMARK_VOICE_TEMPLATE_DEFAULT,
    ),
    SettingSpec(
        "BEACON_WATERMARK_FRAME_TEMPLATE",
        "Beacon — Watermark",
        "Frame template",
        "str.format-style template for the watermark message when "
        "BEACON_TYPE=frame, wrapped as CALLSIGN>DEST:template like any "
        "other frame. Same placeholders as the voice template above. "
        "Subject to the same 256-byte AX.25 hard limit -- an over-length "
        "render is dropped and logged rather than transmitted.",
        "text",
        BEACON_WATERMARK_FRAME_TEMPLATE_DEFAULT,
    ),
    # --- Beacon — Queue ---
    SettingSpec(
        "BEACON_QUEUE_MAX_SIZE",
        "Beacon — Queue",
        "Max pending transmit rows per kind",
        "Max pending beacon_tx_schedule rows held per kind (voice/frame) "
        "before the oldest is dropped to make room for new content. Frame "
        "content is one row per CHUNK, not per item — a real SENAPRED "
        "report has produced 57 chunks on its own, so keep this comfortably "
        "above the largest item you expect. Applies to newly scheduled "
        "items — no restart needed.",
        "int",
        BEACON_QUEUE_MAX_SIZE_DEFAULT,
    ),
    SettingSpec(
        "BEACON_MAX_QUEUED_AGE_SECONDS",
        "Beacon — Queue",
        "Max queued age (s)",
        "A scheduled transmission that has waited this long without going on "
        "air is dropped unsent (audited as beacon.tx.skipped_stale) instead "
        "of transmitted — so turning transmit off overnight doesn't dump a "
        "stale backlog on air when it comes back. Measured from the row's "
        "last enqueue/rearm/send, so a deliberate Re-transmit resets the "
        "clock. Checked every tick whether or not the beacon is enabled. "
        "\"0\" disables the check. Does not affect manual \"Transmit now\" "
        "messages. Default 21600 (6 h).",
        "int",
        BEACON_MAX_QUEUED_AGE_SECONDS_DEFAULT,
    ),
    SettingSpec(
        "BEACON_CONTENT_READY_RECONCILE_INTERVAL_SECONDS",
        "Beacon — Queue",
        "Reconcile interval (s)",
        "How often beacon checks for an item.content_ready publish its "
        "own MQTT subscription missed (e.g. a message published while "
        "briefly offline, or a startup race) and enqueues it anyway. "
        "Runs once immediately on startup regardless of this interval.",
        "int",
        "30",
        advanced=True,
    ),
    # --- Beacon — MQ ---
    SettingSpec(
        "BEACON_MQ_HOST",
        "Beacon — MQ",
        "MQTT host",
        "Broker host beacon subscribes to for incoming content.",
        "text",
        "localhost",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_MQ_PORT",
        "Beacon — MQ",
        "MQTT port",
        "Broker port for beacon.",
        "int",
        "1883",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_MQ_QOS",
        "Beacon — MQ",
        "MQTT QoS",
        "QoS level for beacon's subscriptions.",
        "int",
        "1",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_MQ_RECONNECT_BACKOFF_SECONDS",
        "Beacon — MQ",
        "Reconnect backoff (s)",
        "Seconds between beacon's MQTT reconnect attempts.",
        "int",
        "5",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_CONTENT_READY_SUBSCRIBE_TOPIC",
        "Beacon — MQ",
        "Content-ready subscribe topic",
        "The single trigger feeding both the voice and frame queues — "
        "published by actions.content_ready once both chunk and AI have "
        "finished with an item, avoiding the race where AX.25 would send "
        "raw chunked text before an AI summary was ready.",
        "text",
        "radiobeacon/events/item.content_ready",
        wiring=True,
    ),
    # --- Beacon — NTP ---
    SettingSpec(
        "BEACON_NTP_SERVER",
        "Beacon — NTP",
        "NTP server",
        "Queried for clock-offset visibility only — never used to correct "
        "the clock, which stays the OS's own NTP daemon's job.",
        "text",
        "pool.ntp.org",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_NTP_CHECK_INTERVAL_SECONDS",
        "Beacon — NTP",
        "Check interval (s)",
        "How often to query the NTP server for the current offset.",
        "int",
        "3600",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_NTP_MAX_OFFSET_SECONDS",
        "Beacon — NTP",
        "Max offset before warning (s)",
        "Log loudly (does not block transmission) if the measured clock "
        "offset exceeds this.",
        "float",
        "2.0",
        advanced=True,
    ),
    # --- Setup ---
    # Internal completion flag for the /setup first-run wizard (see
    # ui.setup) — not an operator-meaningful setting, so deliberately left
    # out of NAV_CATEGORY_ORDER (no /config tab). Still a normal DB-backed
    # setting under the hood: reachable at /config/setup and resettable via
    # the usual per-field reset endpoint if the wizard ever needs to be
    # forced to re-run outside its own sidebar "reopen" link.
    SettingSpec(
        "SETUP_WIZARD_COMPLETED",
        "Setup",
        "Setup wizard completed",
        "Set once the first-run setup wizard (/setup) has been completed. "
        "Not meant to be edited directly.",
        "bool",
        None,
        env_fallback=False,
        advanced=True,
    ),
]

GROUPS: list[str] = list(dict.fromkeys(spec.group for spec in SETTINGS_CATALOG))

_KEY_TO_SPEC: dict[str, SettingSpec] = {spec.key: spec for spec in SETTINGS_CATALOG}


def spec_for_key(key: str) -> SettingSpec | None:
    """Looks up a single SettingSpec by its exact key, regardless of which
    group it belongs to — used by ui.setup's wizard steps, which curate a
    handful of keys across several existing groups rather than showing a
    whole group verbatim."""
    return _KEY_TO_SPEC.get(key)


def _slugify(text: str) -> str:
    return text.lower().replace(" — ", "-").replace(" ", "-")


def group_slug(group: str) -> str:
    """e.g. "Actions — AI" -> "actions-ai" — used in /config/{group} URLs."""
    return _slugify(group)


_SLUG_TO_GROUP: dict[str, str] = {group_slug(g): g for g in GROUPS}


def category_for_group(group: str) -> str:
    """"Beacon — Transmission" -> "Beacon", "Dispatcher" -> "Dispatcher" — the
    " — " convention already used throughout GROUPS doubles as a
    category/subcategory split, so /config's category layer needs no new
    metadata on SettingSpec."""
    return group.split(" — ")[0]


CATEGORIES: list[str] = list(dict.fromkeys(category_for_group(g) for g in GROUPS))


def category_slug(category: str) -> str:
    """e.g. "Beacon" -> "beacon" — used as the /config nav tab's URL slug."""
    return _slugify(category)


def groups_for_category(category: str) -> list[str]:
    return [g for g in GROUPS if category_for_group(g) == category]


_SLUG_TO_CATEGORY: dict[str, str] = {category_slug(c): c for c in CATEGORIES}


def category_for_slug(slug: str) -> str | None:
    """Inverse of category_slug — used by /config/{slug} to decide whether a
    path segment names a whole category (Settings tab) rather than a single
    group. Distinct from _SLUG_TO_GROUP: a category with exactly one group
    happens to share its slug with that group (e.g. "dispatcher"), which is
    resolved by the caller preferring the group-edit page in that case."""
    return _SLUG_TO_CATEGORY.get(slug)


def is_multi_group_category(category: str) -> bool:
    """True when a category has more than one settings group and therefore
    needs its own tab-landing page listing them; a single-group category's
    tab IS that group's edit form directly (same slug either way)."""
    return len(groups_for_category(category)) > 1


@dataclass(frozen=True)
class Section:
    """One labelled cluster of groups on a multi-group category's landing
    page (see CATEGORY_LAYOUT). Purely presentational — grouping/ordering
    only, no effect on routing, storage, or a group's own edit form (which
    always shows its own full field list regardless of section)."""

    label: str  # "" renders with no header — the CATEGORY_LAYOUT fallback
    groups: tuple[str, ...]  # exact group names, in display order
    collapsed: bool = False  # rendered inside a closed <details> by default


# Declarative section layout for the categories whose group count actually
# benefits from it (currently Beacon: 11 groups: Identity, Transmission,
# SvxLink, Templates, AX.25, Direwolf, Voice, Watermark, Queue, MQ, NTP; and
# Actions: 3). A category not listed here (MQ's 2 groups, or any future
# small one) falls back to a single unlabeled section via
# sections_for_category — this map is an ordering/labelling aid, not a
# structural requirement. test_config_catalog.py asserts every group of a
# listed category is placed in exactly one of its sections, so a group added
# to SETTINGS_CATALOG without a matching layout update fails a test instead
# of silently landing in an "Other" catch-all (see sections_for_category).
CATEGORY_LAYOUT: dict[str, tuple[Section, ...]] = {
    "Beacon": (
        Section("Station", ("Beacon — Identity", "Beacon — Transmission", "Beacon — Queue")),
        Section("Content", ("Beacon — Templates", "Beacon — Voice", "Beacon — Watermark")),
        Section("Radio hand-off", ("Beacon — SvxLink", "Beacon — AX.25", "Beacon — Direwolf")),
        Section("Infrastructure", ("Beacon — MQ", "Beacon — NTP"), collapsed=True),
    ),
    "Actions": (
        Section("Pipeline", ("Actions — AI", "Actions — Chunk")),
        Section("Infrastructure", ("Actions — Content Ready",), collapsed=True),
    ),
}


def sections_for_category(category: str) -> list[Section]:
    """CATEGORY_LAYOUT's entry for `category`, or one unlabeled section
    holding every group when the category isn't in the map. Any group
    belonging to a *listed* category that no declared Section names is
    appended as a trailing "Other" section — a defensive fallback (kept in
    sync with the catalog by test_config_catalog.py) rather than a silently
    dropped group."""
    all_groups = groups_for_category(category)
    declared = CATEGORY_LAYOUT.get(category)
    if not declared:
        return [Section("", tuple(all_groups))]
    placed = {g for section in declared for g in section.groups}
    leftover = tuple(g for g in all_groups if g not in placed)
    sections = list(declared)
    if leftover:
        sections.append(Section("Other", leftover))
    return sections


def section_slug(label: str) -> str:
    """Anchor id for a section's jump-nav link — "" (the unlabeled fallback)
    still needs a valid id."""
    return _slugify(label) or "general"


# Explicit tab order for the reorganized /config page — deliberately not
# CATEGORIES' own first-occurrence-in-SETTINGS_CATALOG order (which would
# bury Beacon after Secrets/Display/UI); Adapters is still listed here even
# though its tab is actually owned by ui.routers.adapters (merging this
# category's one group with the adapter-instance CRUD into a single
# section), so the nav can treat every tab uniformly.
NAV_CATEGORY_ORDER: list[str] = [
    "Adapters",
    "Dispatcher",
    "MQ",
    "Actions",
    "Beacon",
    "Secrets",
    "Display",
    "UI",
    "Logging",
]


def specs_for_group(slug: str) -> list[SettingSpec]:
    group = _SLUG_TO_GROUP.get(slug)
    if group is None:
        return []
    return [spec for spec in SETTINGS_CATALOG if spec.group == group]


def is_advanced(spec: SettingSpec) -> bool:
    """Whether a setting belongs on the /config "Advanced" sub-tab rather than
    the default "Settings" tab — its own advanced flag, or wiring (which is
    always advanced too). The per-group edit form still shows the whole group
    regardless; this only splits the browse/list view."""
    return spec.advanced or spec.wiring


def group_is_advanced(slug: str) -> bool:
    """True when every spec in the group is advanced — used to point a group
    edit form's back-crumb at the tab that actually lists it."""
    specs = specs_for_group(slug)
    return bool(specs) and all(is_advanced(spec) for spec in specs)
