"""Publishes select dispatcher audit events to a local MQTT broker (see
../../mq/) as CloudEvents — structured content mode: the full envelope,
JSON-encoded, as the MQTT payload (CloudEvents' MQTT protocol binding).
Registered as an adapters.storage audit-event hook by dispatcher's 3
process entry points (__main__.py, policies.py, override_item.py) — never
imported/registered by dispatcher.watcher/policy/override themselves, so
tests importing those modules directly never trigger MQTT side effects.

paho-mqtt and cloudevents are imported lazily, inside functions, so
importing this module has no hard dependency on either package being
installed — only actually publishing (DISPATCHER_MQ_HOST set, an event
type in _PUBLISHED_EVENT_TYPES) does. (cloudevents.http.CloudEvent is used
here despite the "http" in its name — the CloudEvents Python SDK's
envelope construction/conversion utilities are transport-agnostic; "http"
just reflects HTTP being the first binding the SDK implemented.)"""

import json
import logging
import socket
from contextlib import contextmanager
from typing import Any

from adapters.storage import get_setting

logger = logging.getLogger(__name__)

_PUBLISHED_EVENT_TYPES = frozenset(
    {
        "item.dispatched",
        "item.dispatch_failed",
        "item.discovered",
        "item.policy_drifted",
        "item.policy_overridden",
        "item.rearmed",
    }
)

# The dispatcher now delivers each item exactly once (the "put it on air N
# times, spaced out" concern moved to beacon/'s beacon_tx_schedule), so
# item.dispatched fires once per item and every one is published — no
# first-delivery filter needed any more.

_client = None


def _topic(event_type: str) -> str:
    return f"radiobeacon/events/{event_type}"


@contextmanager
def _bounded_socket_timeout(seconds: float):
    """paho's Client.connect() exposes no connect-timeout parameter of its
    own; this bounds the underlying socket connect so a broker that
    accepts the TCP connection but never completes the MQTT handshake
    can't hang dispatcher's single-threaded poll loop indefinitely. Only
    wraps the (lazy, one-time) connect — not every publish, since the
    connection is cached and reused after that."""
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


def _get_client():
    """Returns a cached, already-connected MQTT client, connecting lazily
    on first use. dispatch_due_items can publish many events in a single
    poll cycle (e.g. after a policy edit re-arms a large backlog) —
    reusing one persistent connection instead of paying a fresh TCP+MQTT
    handshake per event (as paho.mqtt.publish.single() would) keeps that
    from serially stalling the single-threaded poll loop. Raises on
    failure — callers must catch (publish_cloud_event does).

    Host/port/timeout are resolved via get_setting() once per connect, not
    once per publish — so a settings-table override to DISPATCHER_MQ_HOST/
    _PORT only takes effect on the next fresh connection (after the current
    one drops and publish_cloud_event's except block resets _client to
    None), not instantly. Not a regression versus env-var-only config,
    which needed a full process restart to see a change at all."""
    global _client
    if _client is not None:
        return _client

    import paho.mqtt.client as mqtt_client

    host = get_setting("DISPATCHER_MQ_HOST")
    port = int(get_setting("DISPATCHER_MQ_PORT", "1883"))
    timeout = int(get_setting("DISPATCHER_MQ_CONNECT_TIMEOUT_SECONDS", "5"))

    client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
    with _bounded_socket_timeout(timeout):
        client.connect(host, port)
    client.loop_start()
    _client = client
    return _client


def publish_cloud_event(
    *,
    event_type: str,
    actor: str,
    source: str | None,
    item_id: str | None,
    details: dict[str, Any] | None,
) -> None:
    """The registered adapters.storage audit-event hook. Filters to the 6
    published event types first (cheap — no env lookup, no import) before
    doing anything else. No-ops entirely if DISPATCHER_MQ_HOST is unset (no
    paho-mqtt import, no connection attempt — same "leave unset to
    disable" convention the rest of this repo uses). Never raises: any
    failure (broker down, timeout, etc.) is logged and swallowed —
    publishing is best-effort on top of the audit_log row, which is
    already committed by the time this runs."""
    if event_type not in _PUBLISHED_EVENT_TYPES:
        return

    host = get_setting("DISPATCHER_MQ_HOST")
    if not host:
        return

    try:
        from cloudevents.conversion import to_dict
        from cloudevents.http import CloudEvent

        event = CloudEvent(
            {
                "type": f"cl.radiobeacon.{event_type}",
                "source": f"radiobeacon/{actor}",
            },
            {
                "source": source,
                "item_id": item_id,
                "actor": actor,
                "details": details or {},
            },
        )
        # ensure_ascii=False: json.dumps() otherwise escapes every
        # non-ASCII character to a \uXXXX sequence by default — valid
        # JSON either way, but unreadable on the wire/in logs for this
        # repo's largely Spanish-language content.
        payload = json.dumps(to_dict(event), ensure_ascii=False)
        qos = int(get_setting("DISPATCHER_MQ_QOS", "1"))
        timeout = int(get_setting("DISPATCHER_MQ_CONNECT_TIMEOUT_SECONDS", "5"))

        client = _get_client()
        info = client.publish(_topic(event_type), payload=payload, qos=qos)
        # client.publish() only queues the message for loop_start()'s
        # background thread to actually send — unlike the old
        # mqtt_publish.single() (which blocked until the broker
        # acknowledged), returning here immediately would let dispatcher's
        # poll loop move on (and, at shutdown, the process could exit)
        # before the message is ever put on the wire. wait_for_publish()
        # blocks just long enough to confirm it was actually sent.
        info.wait_for_publish(timeout=timeout)
    except Exception:
        logger.error("failed to publish CloudEvent for %s", event_type, exc_info=True)
        global _client
        _client = None
