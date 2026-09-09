import importlib
import inspect
import logging
import pkgutil
import signal
import threading

from adapters.logsetup import configure_logging, refresh_level
from adapters.storage import (
    DEFAULT_DB_PATH,
    get_connection,
    get_setting,
    record_audit_event,
)

import actions
from actions import content_ready, mq
from actions.base import Action

logger = logging.getLogger("actions")

# Diagnostic keys a single-output action may attach for humans reading the
# /audit page — copied verbatim from the output payload into the
# action.<name>.executed audit row. `summary` is deliberately NOT here: it
# is already persisted in items.summary and would only bloat the row.
# `prompt` only appears in ai's output when ACTIONS_AI_EVENT_INCLUDE_PROMPT
# is on, so it lands in the audit row on exactly the same opt-in.
_AUDIT_DETAIL_KEYS = ("reason", "provider", "model", "prompt")

# MQ is this package's entire job (unlike dispatcher, where publishing is
# an optional bonus on top of its real job of SQLite polling) — defaults
# to the local broker mq/start.sh brings up rather than requiring
# explicit opt-in, unlike DISPATCHER_MQ_HOST.
ACTIONS_MQ_HOST = get_setting("ACTIONS_MQ_HOST", "localhost")
ACTIONS_MQ_PORT = int(get_setting("ACTIONS_MQ_PORT", "1883"))
ACTIONS_MQ_QOS = int(get_setting("ACTIONS_MQ_QOS", "1"))
ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS = int(
    get_setting("ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS", "5")
)


def discover_actions() -> list[type[Action]]:
    """Finds every concrete Action subclass in this package's submodules,
    so new actions (each implementing the common contract in base.py) are
    picked up automatically without editing this file."""
    classes: list[type[Action]] = []
    for module_info in pkgutil.iter_modules(actions.__path__, actions.__name__ + "."):
        module = importlib.import_module(module_info.name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Action) and obj is not Action and not inspect.isabstract(obj):
                classes.append(obj)
    return list(dict.fromkeys(classes))


def _env_name(action_class: type[Action]) -> str:
    """e.g. actions.chunk.ChunkAction -> "CHUNK", derived from the action's
    module name so adding a new action doesn't require touching this
    file — same derivation the adapters runner uses for its own poll cadence."""
    return action_class.__module__.rsplit(".", 1)[-1].upper()


def _subscribe_topics(action_class: type[Action]) -> list[str] | None:
    """ACTIONS_<NAME>_SUBSCRIBE_TOPIC, comma-separated for multiple
    topics. Resolves DB row -> env var -> the action's declared
    default_subscribe_topic (see base.Action) — so the built-in ai/chunk
    pipeline flows on a fresh install with nothing configured. Returns
    None (the caller logs a warning and skips this action, rather than
    crashing the whole process) when an action declares no default and
    none is configured, or when the resolved value is an explicit empty
    string — the deliberate per-action disable switch."""
    raw = get_setting(
        f"ACTIONS_{_env_name(action_class)}_SUBSCRIBE_TOPIC",
        action_class.default_subscribe_topic,
    )
    if not raw:
        return None
    return [topic.strip() for topic in raw.split(",") if topic.strip()]


def _output_config(action_class: type[Action]) -> tuple[str | None, str]:
    """(output_topic, output_event_type), each DB row -> env var ->
    action-declared default (see base.Action). output_topic is optional —
    an action can legitimately have nowhere to publish (a terminal action
    whose run() always returns [], or output that's intentionally
    dropped), so it stays None when neither configured nor declared.
    output_event_type falls back to the declared default_output_event_type
    and, failing that, the action's own module leaf name."""
    name = _env_name(action_class)
    output_topic = get_setting(
        f"ACTIONS_{name}_OUTPUT_TOPIC", action_class.default_output_topic
    )
    output_event_type = get_setting(
        f"ACTIONS_{name}_OUTPUT_EVENT_TYPE",
        action_class.default_output_event_type or name.lower(),
    )
    return output_topic, output_event_type


def _already_processed(conn, name: str, event_id: str | None) -> bool:
    """Framework-level idempotency check, shared by every action — not
    each action's own responsibility to remember. Defense-in-depth
    against duplicate delivery of the *same* logical event: dispatcher's
    mq_publisher already filters out item.dispatched redeliveries at the
    source, but this also covers the crash-window case (a delivery whose
    audit/publish committed but whose trigger_dispatches state update
    didn't, replayed on restart) and any other future duplicate-delivery
    source.

    Keyed on the triggering CloudEvent's own `id` (assigned by the
    cloudevents SDK, unique per publish — see mq.build_cloud_event_payload),
    not on (source, item_id) as an earlier version of this check did.
    (source, item_id) alone is wrong: a rearm (dispatcher.override.
    rearm_item) publishes a genuinely NEW item.dispatched CloudEvent for
    the same (source, item_id) — keying on that pair would make this
    action permanently skip an item after its first successful run,
    silently defeating rearm's whole purpose of re-entering it into the
    pipeline. Keying on event_id still catches true duplicates (the exact
    same event redelivered) while correctly treating a rearm's fresh
    event as new work. Uses SQLite's JSON1 json_extract — no schema
    change (details is already a JSON TEXT column). An event with no id
    (shouldn't happen — cloudevents always assigns one — but defensive)
    is never considered "already processed", so it's always attempted."""
    if not event_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = ? "
        "AND json_extract(details, '$.event_id') = ? LIMIT 1",
        (f"action.{name}.executed", event_id),
    ).fetchone()
    return row is not None


def _make_on_message(action_class: type[Action], output_topic: str | None, output_event_type: str):
    """Builds the on_message callback for one action. paho-mqtt re-raises
    any exception an on_message callback doesn't catch (Client's
    suppress_exceptions defaults to False) — in loop_start()'s
    background-thread mode this silently kills that action's network
    thread permanently, with no next iteration to recover on (unlike
    _run_adapter_loop's per-iteration try/except). So everything here —
    parse, DB lookup, run(), publish, audit — is wrapped in one
    unconditional try/except, never allowed to propagate."""
    name = _env_name(action_class).lower()
    log = logging.getLogger(f"actions.{name}")

    def _handler(client, userdata, message) -> None:
        try:
            event = mq.parse_cloud_event(message.payload)
            conn = get_connection(DEFAULT_DB_PATH)
            try:
                refresh_level(conn=conn)
                data = event.get("data") or {}
                event_id = event.get("id")
                if _already_processed(conn, name, event_id):
                    log.info(
                        "already processed, skipping event_id=%s source=%s item_id=%s",
                        event_id,
                        data.get("source"),
                        data.get("item_id"),
                    )
                    return
                outputs = action_class().run(event, conn=conn)
                audit_details = {
                    "input_type": event.get("type"),
                    "output_count": len(outputs),
                    "event_id": event_id,
                }
                # A single-output action may attach diagnostic detail
                # (ai's skip `reason`, the `provider`/`model` used, the
                # rendered `prompt`) — surface it on /audit rather than
                # leaving it only in logs. See _AUDIT_DETAIL_KEYS.
                if len(outputs) == 1:
                    for key in _AUDIT_DETAIL_KEYS:
                        if outputs[0].get(key) is not None:
                            audit_details[key] = outputs[0][key]
                try:
                    record_audit_event(
                        conn,
                        event_type=f"action.{name}.executed",
                        actor=f"actions.{name}",
                        source=data.get("source"),
                        item_id=data.get("item_id"),
                        details=audit_details,
                    )
                except Exception:
                    log.error("failed to record action.executed audit event", exc_info=True)
            finally:
                conn.close()

            if outputs and output_topic:
                for output in outputs:
                    payload = mq.build_cloud_event_payload(
                        event_type=output_event_type,
                        actor=f"actions.{name}",
                        data=output,
                    )
                    client.publish(output_topic, payload=payload, qos=ACTIONS_MQ_QOS)
            elif outputs and not output_topic:
                log.warning(
                    "produced %d output(s) but no OUTPUT_TOPIC configured, dropped",
                    len(outputs),
                )
        except Exception:
            log.error("failed handling message topic=%s", message.topic, exc_info=True)

    return _handler


def _run_action_loop(
    action_class: type[Action],
    topics: list[str],
    output_topic: str | None,
    output_event_type: str,
    stop_event: threading.Event,
) -> None:
    """Runs one action forever, independently of every other action's
    loop — one MQTT connection per action (full failure isolation, mirrors
    adapters' one-thread-per-unit model), not a client shared across
    actions. Uses loop_start() (paho's own background network thread) +
    stop_event.wait(), not loop_forever() — loop_forever() blocks
    uninterruptibly until disconnect() is called from inside a callback,
    with no way to signal it from a threading.Event like every other loop
    in this repo does.

    clean_session=True (a stable client_id, but NOT a persistent MQTT
    session): a broker restart or reconnect starts this client with zero
    subscriptions carried over, so it always ends up subscribed to
    exactly ACTIONS_<NAME>_SUBSCRIBE_TOPIC's CURRENT value — nothing
    more. This was flipped from clean_session=False (this repo's earlier
    choice) after a real incident: MQTT SUBSCRIBE is purely additive, so
    a persistent session across a topic rename (e.g. chunk's own
    ACTIONS_CHUNK_SUBSCRIBE_TOPIC changing from item.dispatched to
    item.ai_settled earlier this session) kept the OLD subscription
    alive forever alongside the new one — confirmed directly in the
    broker's own persistence file — silently double-triggering this
    action on every dispatch (chunking raw content AND the AI summary,
    both, for the same item). A message published while this action is
    briefly offline is now lost rather than queued — recoverable via a
    rearm, the same manual-recovery path this repo already relies on for
    dispatcher redelivery."""
    import paho.mqtt.client as mqtt_client

    name = _env_name(action_class).lower()
    log = logging.getLogger(f"actions.{name}")
    log.info("starting topics=%s", topics)

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        client_id=f"radiobeacon-actions-{name}",
        clean_session=True,
    )
    client.on_message = _make_on_message(action_class, output_topic, output_event_type)

    def _on_connect(client, userdata, connect_flags, reason_code, properties) -> None:
        # paho's built-in reconnect_on_failure (default True) restores the
        # TCP/MQTT connection automatically after a drop, but does NOT
        # restore subscriptions on its own — without re-subscribing here,
        # this action would silently stop receiving messages forever
        # after the very first broker restart/network blip, with no
        # error logged anywhere. on_connect fires on every successful
        # (re)connect, so this covers both the initial connect and every
        # later reconnect.
        log.debug("connected, subscribing reason_code=%s topics=%s", reason_code, topics)
        client.subscribe([(topic, ACTIONS_MQ_QOS) for topic in topics])

    client.on_connect = _on_connect

    while not stop_event.is_set():
        try:
            client.connect(ACTIONS_MQ_HOST, ACTIONS_MQ_PORT)
            break
        except Exception:
            log.error(
                "failed to connect to %s:%d, retrying in %ds",
                ACTIONS_MQ_HOST,
                ACTIONS_MQ_PORT,
                ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS,
                exc_info=True,
            )
            stop_event.wait(ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS)
    else:
        log.info("stopped before connecting")
        return

    client.loop_start()
    stop_event.wait()
    client.loop_stop()
    client.disconnect()
    log.info("stopped")


def _run_content_ready_loop(stop_event: threading.Event) -> None:
    """Runs actions.content_ready's poll loop forever, on its own MQTT
    connection (only needs to publish, never subscribes) — same
    connect/loop_start/stop_event.wait shape as _run_action_loop, but
    ticking on a plain interval instead of reacting to messages."""
    import paho.mqtt.client as mqtt_client

    poll_interval = int(get_setting("ACTIONS_CONTENT_READY_POLL_INTERVAL_SECONDS", "2"))
    output_topic = get_setting(
        "ACTIONS_CONTENT_READY_OUTPUT_TOPIC", "radiobeacon/events/item.content_ready"
    )
    actor = "actions.content_ready"
    log = logging.getLogger("actions.content_ready")

    log.info("starting poll_interval=%ds topic=%s", poll_interval, output_topic)

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        client_id="radiobeacon-actions-content_ready",
        clean_session=True,  # publish-only client, no subscriptions to persist
    )

    while not stop_event.is_set():
        try:
            client.connect(ACTIONS_MQ_HOST, ACTIONS_MQ_PORT)
            break
        except Exception:
            log.error(
                "failed to connect to %s:%d, retrying in %ds",
                ACTIONS_MQ_HOST, ACTIONS_MQ_PORT, ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS,
                exc_info=True,
            )
            stop_event.wait(ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS)
    else:
        log.info("stopped before connecting")
        return

    client.loop_start()
    try:
        while not stop_event.is_set():
            conn = get_connection(DEFAULT_DB_PATH)
            try:
                refresh_level(conn=conn)
                published = content_ready.check_and_publish(
                    conn, client, output_topic=output_topic, actor=actor, qos=ACTIONS_MQ_QOS
                )
                if published:
                    log.info("published count=%d", published)
            except Exception:
                log.error("poll tick failed", exc_info=True)
            finally:
                conn.close()
            stop_event.wait(poll_interval)
    finally:
        client.loop_stop()
        client.disconnect()
        log.info("stopped")


def main() -> None:
    action_classes = discover_actions()
    if not action_classes:
        logger.warning("no actions found")

    stop_event = threading.Event()

    def _handle_shutdown_signal(signum, frame) -> None:
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    threads = []
    for action_class in action_classes:
        name = _env_name(action_class).lower()
        topics = _subscribe_topics(action_class)
        if topics is None:
            logger.warning("no SUBSCRIBE_TOPIC configured, skipping action=%s", name)
            continue
        output_topic, output_event_type = _output_config(action_class)
        threads.append(
            threading.Thread(
                target=_run_action_loop,
                args=(action_class, topics, output_topic, output_event_type, stop_event),
                name=action_class.__name__,
                daemon=True,
            )
        )

    if not threads:
        logger.warning("no actions configured (every discovered action is missing SUBSCRIBE_TOPIC)")

    threads.append(
        threading.Thread(
            target=_run_content_ready_loop,
            args=(stop_event,),
            name="ContentReady",
            daemon=True,
        )
    )

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()


if __name__ == "__main__":
    configure_logging("actions")
    main()
