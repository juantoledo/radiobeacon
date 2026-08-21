"""CloudEvent helpers shared by every action's message handling — package-
local rather than reusing dispatcher/src/dispatcher/mq_publisher.py, since
that module is fixed to dispatcher's own 6-event whitelist and its
adapters.storage audit-hook registration model, neither of which fits an
action subscribing to arbitrary configured topics.

cloudevents is imported lazily, inside functions, so importing this module
has no hard dependency on it being installed unless actually parsing/
building a CloudEvent. (cloudevents.http.CloudEvent is used here despite
the "http" in its name — the CloudEvents Python SDK's envelope
construction/conversion utilities are transport-agnostic; "http" just
reflects HTTP being the first binding the SDK implemented — same
convention as mq_publisher.py.)"""

import json
from typing import Any


def parse_cloud_event(payload: bytes | str) -> dict[str, Any]:
    """Parses a structured-mode CloudEvents JSON MQTT payload (the shape
    mq_publisher.py's build_cloud_event_payload()/publish_cloud_event()
    produce) back into a plain dict with the full envelope (specversion,
    type, source, id, time, data, ...). Round-trips through the
    cloudevents SDK's own CloudEvent object rather than just json.loads(),
    so a malformed/non-CloudEvent payload fails the same way real
    CloudEvents validation would, not just "silently missing a field"."""
    from cloudevents.conversion import to_dict
    from cloudevents.http import from_dict

    raw = json.loads(payload)
    event = from_dict(raw)
    return to_dict(event)


def build_cloud_event_payload(*, event_type: str, actor: str, data: dict[str, Any]) -> str:
    """Mirrors mq_publisher.publish_cloud_event's envelope construction
    exactly, for consistency across every CloudEvent this repo produces —
    just returns the serialized payload instead of publishing it, since
    each action publishes on its own already-open MQTT client
    (see __main__.py)."""
    from cloudevents.conversion import to_dict
    from cloudevents.http import CloudEvent

    event = CloudEvent(
        {
            "type": f"cl.radiobeacon.{event_type}",
            "source": f"radiobeacon/{actor}",
        },
        data,
    )
    return json.dumps(to_dict(event))
