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
managed at /adapters — see ui.routers.adapters.
UI_HOST/UI_PORT/UI_DB_PATH are the only settings that stay env-only — they're
needed before the app can even reach its own database, so they can never be
DB-backed like everything else here (see ui.config)."""
from dataclasses import dataclass, field

from adapters.beacon_defaults import (
    BEACON_ENABLED_DEFAULT,
    BEACON_QUEUE_MAX_SIZE_DEFAULT,
    BEACON_TYPE_DEFAULT,
    BEACON_VOICE_PREFIX_DEFAULT,
    BEACON_VOICE_SUFFIX_DEFAULT,
    BEACON_VOICE_TEMPLATE_DEFAULT,
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
    # advanced=True: changes pub/sub topology (topic/event-type wiring) —
    # a typo here silently breaks a pipeline with no visible error, unlike
    # a numeric threshold. Rendered with extra warning styling and a
    # confirm-before-save prompt in config_group_form.html.
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
        "Fallback poll interval for any adapter lacking its own "
        "ADAPTERS_<NAME>_INTERVAL_SECONDS.",
        "int",
        "10",
    ),
    # Per-source adapter config (CSN, SENAPRED, and any operator-added
    # instance) no longer lives here — each is a row in the adapter_instances
    # table, managed at /adapters instead of /config. See ui.routers.adapters.
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
    ),
    SettingSpec(
        "DISPATCHER_MQ_PORT",
        "MQ — Dispatcher",
        "MQTT port",
        "Broker port for dispatcher's audit-event publishing.",
        "int",
        "1883",
    ),
    SettingSpec(
        "DISPATCHER_MQ_QOS",
        "MQ — Dispatcher",
        "MQTT QoS",
        "QoS level for dispatcher's published events.",
        "int",
        "1",
    ),
    SettingSpec(
        "DISPATCHER_MQ_CONNECT_TIMEOUT_SECONDS",
        "MQ — Dispatcher",
        "Connect timeout (s)",
        "Timeout for dispatcher's MQTT connect/publish.",
        "int",
        "5",
    ),
    # --- MQ — Actions ---
    SettingSpec(
        "ACTIONS_MQ_HOST",
        "MQ — Actions",
        "MQTT host",
        "Broker host actions subscribe to and publish on.",
        "text",
        "localhost",
    ),
    SettingSpec(
        "ACTIONS_MQ_PORT",
        "MQ — Actions",
        "MQTT port",
        "Broker port for actions.",
        "int",
        "1883",
    ),
    SettingSpec(
        "ACTIONS_MQ_QOS",
        "MQ — Actions",
        "MQTT QoS",
        "QoS level for actions' subscribe/publish.",
        "int",
        "1",
    ),
    SettingSpec(
        "ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS",
        "MQ — Actions",
        "Reconnect backoff (s)",
        "Seconds between actions' MQTT reconnect attempts.",
        "int",
        "5",
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
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_CHUNK_OUTPUT_TOPIC",
        "Actions — Chunk",
        "Output topic",
        "MQTT topic ChunkAction publishes item.chunked events to.",
        "text",
        "radiobeacon/events/item.chunked",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_CHUNK_OUTPUT_EVENT_TYPE",
        "Actions — Chunk",
        "Output event type",
        "CloudEvent `type` string for ChunkAction's output.",
        "text",
        "item.chunked",
        advanced=True,
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
        advanced=True,
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
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_AI_OUTPUT_EVENT_TYPE",
        "Actions — AI",
        "Output event type",
        "CloudEvent `type` string for AiAction's output.",
        "text",
        "item.ai_settled",
        advanced=True,
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
        "{event_key}, {type}, {subtype}, {transmit_policy}, {source_date_time}, "
        "{fetched_at}, {captured_at}, {rawdata}, plus the source's display name and "
        "site URL as {source_name} / {source_url} — an unknown placeholder just renders "
        "blank. A single adapter can override this further via its own 'AI prompt "
        "override' field on the /adapters form.",
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
        advanced=True,
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
        "UI_DEFAULT_CONSUMER_NAME",
        "UI",
        "Default consumer name",
        "Consumer name prefilled on the item detail/edit rearm form. When "
        "unset, falls back to the current effective DISPATCHER_CONSUMER_NAME.",
        "text",
        "log",
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
        "The beacon's amateur radio callsign, e.g. CD3DXZ-1.",
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
        "VHF transmit frequency, e.g. 144.390 MHz.",
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
        "transmitter (see Beacon — Output). Changing this clears any "
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
    # --- Beacon — Output ---
    SettingSpec(
        "BEACON_WAV_TRANSMITTER",
        "Beacon — Output",
        "WAV transmitter",
        "\"logging\" (default, safe) just logs the WAV it would hand off. "
        "\"spool\" drops the WAV into the svxlink-txqueue spool for SvxLink "
        "to play when the RF channel is idle — see "
        "documentation/svxlink-txqueue-SETUP.md.",
        "select",
        "logging",
        choices=("logging", "spool"),
        advanced=True,
    ),
    SettingSpec(
        "BEACON_TXQUEUE_INCOMING_DIR",
        "Beacon — Output",
        "svxlink-txqueue incoming dir",
        "Folder the \"spool\" WAV transmitter drops rendered WAV files into — "
        "svxlink-txqueue's documented drop point, which stamps a FIFO "
        "timestamp, validates, and sanitizes the name. Must match "
        "svxlink-txqueue's TXQUEUE_SPOOL/incoming.",
        "text",
        "/var/spool/svxlink-tx/incoming",
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
        "500",
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
    SettingSpec(
        "BEACON_GEN_PACKETS_BINARY",
        "Beacon — AX.25",
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
        "Beacon — AX.25",
        "Frame lead silence (ms)",
        "Milliseconds of silence prepended to each rendered frame WAV so the "
        "first bits aren't clipped while SvxLink keys the transmitter.",
        "int",
        "250",
        advanced=True,
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
    ),
    SettingSpec(
        "BEACON_TTS_WAV_DIR",
        "Beacon — Voice",
        "TTS output directory",
        "Where synthesized WAV files are written before playback.",
        "text",
        "storage/beacon_tts",
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
    ),
    SettingSpec(
        "BEACON_MQ_PORT",
        "Beacon — MQ",
        "MQTT port",
        "Broker port for beacon.",
        "int",
        "1883",
    ),
    SettingSpec(
        "BEACON_MQ_QOS",
        "Beacon — MQ",
        "MQTT QoS",
        "QoS level for beacon's subscriptions.",
        "int",
        "1",
    ),
    SettingSpec(
        "BEACON_MQ_RECONNECT_BACKOFF_SECONDS",
        "Beacon — MQ",
        "Reconnect backoff (s)",
        "Seconds between beacon's MQTT reconnect attempts.",
        "int",
        "5",
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
        advanced=True,
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
    ),
    SettingSpec(
        "BEACON_NTP_CHECK_INTERVAL_SECONDS",
        "Beacon — NTP",
        "Check interval (s)",
        "How often to query the NTP server for the current offset.",
        "int",
        "3600",
    ),
    SettingSpec(
        "BEACON_NTP_MAX_OFFSET_SECONDS",
        "Beacon — NTP",
        "Max offset before warning (s)",
        "Log loudly (does not block transmission) if the measured clock "
        "offset exceeds this.",
        "float",
        "2.0",
    ),
]

GROUPS: list[str] = list(dict.fromkeys(spec.group for spec in SETTINGS_CATALOG))


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
    """e.g. "Beacon" -> "beacon" — used as the /config page's jump-nav/
    <details> section anchor id."""
    return _slugify(category)


def groups_for_category(category: str) -> list[str]:
    return [g for g in GROUPS if category_for_group(g) == category]


def specs_for_group(slug: str) -> list[SettingSpec]:
    group = _SLUG_TO_GROUP.get(slug)
    if group is None:
        return []
    return [spec for spec in SETTINGS_CATALOG if spec.group == group]
