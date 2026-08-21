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
import os
import socket
from contextlib import contextmanager
from typing import Any

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


def _topic(event_type: str) -> str:
    return f"radiobeacon/events/{event_type}"


@contextmanager
def _bounded_socket_timeout(seconds: float):
    """paho.mqtt.publish.single() opens/closes its own connection per call
    and exposes no connect-timeout parameter of its own; this bounds the
    underlying socket connect so a broker that accepts the TCP connection
    but never completes the MQTT handshake can't hang dispatcher's
    single-threaded poll loop indefinitely."""
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


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

    host = os.environ.get("DISPATCHER_MQ_HOST")
    if not host:
        return

    try:
        from cloudevents.conversion import to_dict
        from cloudevents.http import CloudEvent
        import paho.mqtt.publish as mqtt_publish

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
        payload = json.dumps(to_dict(event))
        port = int(os.environ.get("DISPATCHER_MQ_PORT", "1883"))
        qos = int(os.environ.get("DISPATCHER_MQ_QOS", "1"))
        timeout = int(os.environ.get("DISPATCHER_MQ_CONNECT_TIMEOUT_SECONDS", "5"))

        with _bounded_socket_timeout(timeout):
            mqtt_publish.single(
                _topic(event_type),
                payload=payload,
                qos=qos,
                hostname=host,
                port=port,
            )
    except Exception:
        logger.error("failed to publish CloudEvent for %s", event_type, exc_info=True)
