"""CloudEvent parsing helper — package-local, same reasoning as
actions/src/actions/mq.py's own docstring: each package that consumes
CloudEvents gets its own small helper rather than reusing another
package's, since beacon doesn't add actions/src (or dispatcher/src) to its
own sys.path, only data-adapters/src (for adapters.storage). beacon only
ever parses incoming events (it doesn't publish its own — see
__main__.py's module docstring), so unlike actions.mq this has no
build_cloud_event_payload."""
import json
from typing import Any


def parse_cloud_event(payload: bytes | str) -> dict[str, Any]:
    """Parses a structured-mode CloudEvents JSON MQTT payload back into a
    plain dict with the full envelope (specversion, type, source, id,
    time, data, ...). Round-trips through the cloudevents SDK's own
    CloudEvent object rather than just json.loads(), so a malformed/
    non-CloudEvent payload fails the same way real CloudEvents validation
    would."""
    from cloudevents.conversion import to_dict
    from cloudevents.http import from_dict

    raw = json.loads(payload)
    event = from_dict(raw)
    return to_dict(event)
