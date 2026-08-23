"""Static catalog of the env-var-backed settings the /config section exposes
for editing — presentation metadata only (labels, form widget type,
grouping), never imported outside this package. The actual read/write
happens through adapters.storage.get_setting/set_setting/list_settings,
keyed by the exact env var name each SettingSpec.key names.

Scope is deliberately v1-limited: adapters (senapred, csn), actions (chunk,
ai), dispatcher, MQ infra, and the two provider secrets. UI_* settings and
DISPLAY_TIMEZONE are out of scope for now — ui.config stays env-only, and
DISPLAY_TIMEZONE would naturally join a future "Display"/"General" group
alongside UI_* if that scope is ever added."""
from dataclasses import dataclass, field


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
    # --- Adapters — SENAPRED ---
    SettingSpec(
        "ADAPTERS_SENAPRED_INTERVAL_SECONDS",
        "Adapters — SENAPRED",
        "Poll interval (s)",
        "How often the SENAPRED adapter polls for new alerts.",
        "int",
        "10",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_IDENTITY_POOL_ID",
        "Adapters — SENAPRED",
        "Cognito identity pool ID",
        "AWS Cognito Identity Pool ID used for SENAPRED's anonymous-visitor auth.",
        "text",
        "us-east-1:17c696bc-53e1-49a2-991f-f1b65f752fda",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_COGNITO_REGION",
        "Adapters — SENAPRED",
        "Cognito region",
        "AWS region for the Cognito identity pool above.",
        "text",
        "us-east-1",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_APPSYNC_REGION",
        "Adapters — SENAPRED",
        "AppSync region",
        "AWS region used for AppSync GraphQL SigV4 request signing.",
        "text",
        "us-east-1",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_APPSYNC_HOST",
        "Adapters — SENAPRED",
        "AppSync host",
        "AppSync GraphQL API host (no scheme/path).",
        "text",
        "rz2uv7ifxbgflh2bqmp6kmh4le.appsync-api.us-east-1.amazonaws.com",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_ALERTA_BASE_URL",
        "Adapters — SENAPRED",
        "Alerta base URL",
        "Base URL used to build public links for 'Alerta'-type items.",
        "text",
        "https://senapred.cl/alerta/",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_EVENTO_BASE_URL",
        "Adapters — SENAPRED",
        "Evento base URL",
        "Base URL used to build public links for 'Evento'-type items.",
        "text",
        "https://senapred.cl/evento/",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_QUERY_LIMIT",
        "Adapters — SENAPRED",
        "Query limit",
        "Max items fetched per SENAPRED feed call (Alerta + Evento each).",
        "int",
        "20",
    ),
    # --- Adapters — CSN ---
    SettingSpec(
        "ADAPTERS_CSN_INTERVAL_SECONDS",
        "Adapters — CSN",
        "Poll interval (s)",
        "How often the CSN adapter polls for new earthquakes.",
        "int",
        "10",
    ),
    SettingSpec(
        "ADAPTERS_CSN_API_URL",
        "Adapters — CSN",
        "API URL",
        "CSN earthquake API endpoint (unofficial JSON mirror).",
        "text",
        "https://api.gael.cloud/general/public/sismos",
    ),
    SettingSpec(
        "ADAPTERS_CSN_SITE_URL",
        "Adapters — CSN",
        "Site URL",
        "Public site URL linked from every CSN item.",
        "text",
        "https://www.sismologia.cl/",
    ),
    SettingSpec(
        "ADAPTERS_CSN_URGENT_MAGNITUDE_THRESHOLD",
        "Adapters — CSN",
        "Urgent magnitude threshold",
        "Magnitude at/above which a CSN item gets dispatch_policy=\"urgent\" "
        "instead of \"informational\".",
        "float",
        "4.5",
    ),
    SettingSpec(
        "ADAPTERS_CSN_SOURCE_TZ",
        "Adapters — CSN",
        "Source timezone",
        "IANA timezone assumed for CSN's naive timestamps, used to convert to UTC.",
        "text",
        "America/Santiago",
    ),
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
        "Broker host for dispatcher's audit-event publishing. Unset disables "
        "publishing entirely.",
        "text",
        None,
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
        "settled. Required — action is skipped if unset.",
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
        "MQTT topic AiAction subscribes to.",
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
        "str.format-style override of the default summarization prompt "
        "(placeholders: {extracted_title}, {type}, {subtype}, {extracted_contents}, {url}).",
        "text",
        None,
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
    # --- Beacon — Schedule ---
    # BEACON_ENABLED also gets a first-class Enable/Disable control on
    # /beacon itself (see ui/src/ui/routers/beacon.py) — it stays listed
    # here too for discoverability/consistency with every other setting.
    SettingSpec(
        "BEACON_ENABLED",
        "Beacon — Schedule",
        "Enabled",
        "Whether the beacon TDMA loop actually transmits queued voice/frame "
        "content. Re-read every tick — no restart needed to flip it.",
        "bool",
        "false",
    ),
    SettingSpec(
        "BEACON_WINDOW_TOTAL_SECONDS",
        "Beacon — Schedule",
        "Window total (s)",
        "Length of one full TDMA cycle: voice + guard + frame + idle.",
        "int",
        "90",
    ),
    SettingSpec(
        "BEACON_WINDOW_VOICE_SECONDS",
        "Beacon — Schedule",
        "Voice slot (s)",
        "Seconds of the cycle reserved for voice transmission.",
        "int",
        "60",
    ),
    SettingSpec(
        "BEACON_WINDOW_FRAME_SECONDS",
        "Beacon — Schedule",
        "Frame slot (s)",
        "Seconds of the cycle reserved for AX.25 frame transmission.",
        "int",
        "30",
    ),
    SettingSpec(
        "BEACON_WINDOW_GUARD_SECONDS",
        "Beacon — Schedule",
        "Guard time (s)",
        "Gap between the voice and frame slots, letting the outgoing "
        "transmitter release the shared audio device before the next one "
        "opens it. 0 collapses voice straight into frame.",
        "int",
        "0",
    ),
    SettingSpec(
        "BEACON_TICK_SECONDS",
        "Beacon — Schedule",
        "Tick interval (s)",
        "How often the TDMA loop re-evaluates the schedule. Small relative to "
        "the slot lengths so short slots aren't missed.",
        "int",
        "1",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_SLOT_LEAD_TIME_SECONDS",
        "Beacon — Schedule",
        "Slot lead time (s)",
        "How long before a content slot's start to stop/start SvxLink or "
        "Direwolf, so the target service is ready by the time the slot "
        "actually begins. A placeholder — CONTEXT.md's own PTT_LATENCY_S/ "
        "TNC_LATENCY_S need measuring on the real hardware.",
        "int",
        "2",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_VOICE_INTER_TX_DELAY_SECONDS",
        "Beacon — Schedule",
        "Voice inter-transmission delay (s)",
        "Gap between consecutive voice transmissions when draining a full "
        "backlog within one slot occurrence. A placeholder — needs real "
        "hardware measurement, same as BEACON_SLOT_LEAD_TIME_SECONDS.",
        "float",
        "2",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_FRAME_INTER_TX_DELAY_SECONDS",
        "Beacon — Schedule",
        "Frame inter-transmission delay (s)",
        "Gap between consecutive frame transmissions when draining a full "
        "backlog within one slot occurrence. A placeholder — needs real "
        "hardware measurement, same as BEACON_SLOT_LEAD_TIME_SECONDS.",
        "float",
        "2",
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
        "than crashing the TDMA loop.",
        "text",
        "{callsign}. {text}. {date}",
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
        "prefix/suffix wrap an already-sized chunk.",
        "text",
        "",
    ),
    SettingSpec(
        "BEACON_VOICE_SUFFIX",
        "Beacon — Templates",
        "Voice content suffix",
        "Appended to the resolved voice text before it's substituted "
        "into BEACON_VOICE_TEMPLATE's {text}. Same placeholders and "
        "truncation-budget behavior as BEACON_VOICE_PREFIX.",
        "text",
        "",
    ),
    SettingSpec(
        "BEACON_VOICE_MAX_CHARS",
        "Beacon — Templates",
        "Voice max chars",
        "Max characters of resolved voice text before word-boundary "
        "truncation (first piece only, no part markers — unlike frame "
        "chunking, the rest is silently dropped). A time-budget cap sized "
        "against BEACON_WINDOW_VOICE_SECONDS, not a protocol limit like "
        "frame's. Deliberately separate from ACTIONS_AI_MAX_CHARS, whose "
        "job is gating whether the LLM runs at all.",
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
        "BEACON_AX25_KISS_HOST",
        "Beacon — AX.25",
        "Direwolf KISS host",
        "Host of Direwolf's KISS TCP socket.",
        "text",
        "localhost",
    ),
    SettingSpec(
        "BEACON_AX25_KISS_PORT",
        "Beacon — AX.25",
        "Direwolf KISS port",
        "Port of Direwolf's KISS TCP socket.",
        "int",
        "8001",
    ),
    SettingSpec(
        "BEACON_AX25_CONNECT_TIMEOUT_SECONDS",
        "Beacon — AX.25",
        "Connect timeout (s)",
        "How long to wait when connecting to Direwolf's KISS socket.",
        "int",
        "5",
    ),
    # --- Beacon — Voice ---
    SettingSpec(
        "BEACON_VOICE_TRANSMITTER",
        "Beacon — Voice",
        "Voice transmitter",
        "\"logging\" (default, safe) just logs what would be played. "
        "\"svxlink\" is an unverified stub — see beacon/README.md.",
        "select",
        "logging",
        choices=("logging", "svxlink"),
        advanced=True,
    ),
    SettingSpec(
        "BEACON_TTS_ENGINE",
        "Beacon — Voice",
        "TTS engine",
        "\"espeak\" (default) is offline/zero-setup but sounds robotic. "
        "\"piper\" is offline neural TTS — much more natural, but needs a "
        "downloaded voice model (BEACON_TTS_PIPER_MODEL).",
        "select",
        "espeak",
        choices=("espeak", "piper"),
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
        "BEACON_TTS_ENGINE=piper.",
        "text",
        "",
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
        "Max queue size",
        "Max items held per channel (voice/frame) before the oldest is "
        "dropped to make room for new content. Frame content is one slot "
        "per CHUNK, not per item — a real SENAPRED report has produced 57 "
        "chunks on its own, so keep this comfortably above the largest "
        "item you expect. Applies live — no restart needed.",
        "int",
        "200",
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
    # --- Beacon — Service control ---
    SettingSpec(
        "BEACON_SERVICE_CONTROLLER",
        "Beacon — Service control",
        "Service controller",
        "\"logging\" (default, safe) just logs the start/stop it would "
        "issue. \"systemctl\" actually controls SvxLink/Direwolf via "
        "systemd — requires a scoped passwordless sudoers rule on the "
        "host, see beacon/README.md.",
        "select",
        "logging",
        choices=("logging", "systemctl"),
        advanced=True,
    ),
    SettingSpec(
        "BEACON_SVXLINK_SERVICE_NAME",
        "Beacon — Service control",
        "SvxLink service name",
        "systemd unit name stopped/started around the voice slot.",
        "text",
        "svxlink",
        advanced=True,
    ),
    SettingSpec(
        "BEACON_DIREWOLF_SERVICE_NAME",
        "Beacon — Service control",
        "Direwolf service name",
        "systemd unit name stopped/started around the frame slot.",
        "text",
        "direwolf",
        advanced=True,
    ),
]

GROUPS: list[str] = list(dict.fromkeys(spec.group for spec in SETTINGS_CATALOG))


def group_slug(group: str) -> str:
    """e.g. "Actions — AI" -> "actions-ai" — used in /config/{group} URLs."""
    return group.lower().replace(" — ", "-").replace(" ", "-")


_SLUG_TO_GROUP: dict[str, str] = {group_slug(g): g for g in GROUPS}


def specs_for_group(slug: str) -> list[SettingSpec]:
    group = _SLUG_TO_GROUP.get(slug)
    if group is None:
        return []
    return [spec for spec in SETTINGS_CATALOG if spec.group == group]
