import importlib
import inspect
import logging
import pkgutil
import signal
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "data-adapters" / "src"))

from adapters.storage import (  # noqa: E402
    DEFAULT_DB_PATH,
    get_connection,
    get_setting,
    record_audit_event,
)

import actions  # noqa: E402
from actions import mq  # noqa: E402
from actions.base import Action  # noqa: E402

logger = logging.getLogger(__name__)

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
    picked up automatically without editing this file — same pattern as
    adapters.discover_adapters()."""
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
    file — same derivation as adapters.__main__._interval_seconds()."""
    return action_class.__module__.rsplit(".", 1)[-1].upper()


def _subscribe_topics(action_class: type[Action]) -> list[str] | None:
    """ACTIONS_<NAME>_SUBSCRIBE_TOPIC, comma-separated for multiple
    topics. Required — no sensible generic default exists (unlike e.g.
    ADAPTERS_DEFAULT_INTERVAL_SECONDS), so returns None if unset; the
    caller logs a warning and skips this action rather than crashing the
    whole process over one misconfigured action."""
    raw = get_setting(f"ACTIONS_{_env_name(action_class)}_SUBSCRIBE_TOPIC")
    if not raw:
        return None
    return [topic.strip() for topic in raw.split(",") if topic.strip()]


def _output_config(action_class: type[Action]) -> tuple[str | None, str]:
    """(output_topic, output_event_type). output_topic is optional — an
    action can legitimately have nowhere to publish (a terminal action
    whose run() always returns [], or output that's intentionally
    dropped); output_event_type defaults to the action's own module leaf
    name if not set explicitly."""
    name = _env_name(action_class)
    output_topic = get_setting(f"ACTIONS_{name}_OUTPUT_TOPIC")
    output_event_type = get_setting(
        f"ACTIONS_{name}_OUTPUT_EVENT_TYPE", name.lower()
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

    def _handler(client, userdata, message) -> None:
        try:
            event = mq.parse_cloud_event(message.payload)
            conn = get_connection(DEFAULT_DB_PATH)
            try:
                data = event.get("data") or {}
                event_id = event.get("id")
                if _already_processed(conn, name, event_id):
                    logger.info(
                        "%s: already processed event_id=%s (source=%s item_id=%s), skipping",
                        name,
                        event_id,
                        data.get("source"),
                        data.get("item_id"),
                    )
                    return
                outputs = action_class().run(event, conn=conn)
                try:
                    record_audit_event(
                        conn,
                        event_type=f"action.{name}.executed",
                        actor=f"actions.{name}",
                        source=data.get("source"),
                        item_id=data.get("item_id"),
                        details={
                            "input_type": event.get("type"),
                            "output_count": len(outputs),
                            "event_id": event_id,
                        },
                    )
                except Exception:
                    logger.error(
                        "failed to record audit event for action.%s.executed",
                        name,
                        exc_info=True,
                    )
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
                logger.warning(
                    "%s produced %d output(s) but no OUTPUT_TOPIC configured; dropped",
                    name,
                    len(outputs),
                )
        except Exception:
            logger.error(
                "%s: failed handling message on %s", name, message.topic, exc_info=True
            )

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
    in this repo does. Uses a stable client_id + clean_session=False (a
    persistent MQTT session): without this, the broker forces a clean
    session for an unnamed client and drops any QoS 1 message published
    while this action is offline — messages would be silently lost
    forever with no replay mechanism, unlike dispatcher's own audit_log/
    rearm_item. A persistent session tells the broker to queue messages
    for this exact client_id until it reconnects."""
    import paho.mqtt.client as mqtt_client

    name = _env_name(action_class).lower()
    logger.info("%s: starting (topics=%s)", name, topics)

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        client_id=f"radiobeacon-actions-{name}",
        clean_session=False,
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
        logger.debug("%s: connected (reason_code=%s), subscribing to %s", name, reason_code, topics)
        client.subscribe([(topic, ACTIONS_MQ_QOS) for topic in topics])

    client.on_connect = _on_connect

    while not stop_event.is_set():
        try:
            client.connect(ACTIONS_MQ_HOST, ACTIONS_MQ_PORT)
            break
        except Exception:
            logger.error(
                "%s: failed to connect to %s:%d, retrying in %ds",
                name,
                ACTIONS_MQ_HOST,
                ACTIONS_MQ_PORT,
                ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS,
                exc_info=True,
            )
            stop_event.wait(ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS)
    else:
        logger.info("%s: stopped before connecting", name)
        return

    client.loop_start()
    stop_event.wait()
    client.loop_stop()
    client.disconnect()
    logger.info("%s: stopped", name)


def main() -> None:
    action_classes = discover_actions()
    if not action_classes:
        logger.warning("no actions found")
        return

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
            logger.warning("%s: no SUBSCRIBE_TOPIC configured, skipping", name)
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
        return

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
