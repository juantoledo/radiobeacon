import json

from actions.mq import build_cloud_event_payload, parse_cloud_event


def test_parse_cloud_event_round_trips_dispatcher_style_payload():
    # Shaped exactly like mq_publisher.publish_cloud_event's real output.
    payload = json.dumps(
        {
            "specversion": "1.0",
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "source": "radiobeacon/log_handler",
            "type": "cl.radiobeacon.item.dispatched",
            "time": "2026-08-21T14:32:07.123456+00:00",
            "data": {
                "source": "senapred",
                "item_id": "abc123",
                "actor": "log_handler",
                "details": {"consumer": "log"},
            },
        }
    )

    event = parse_cloud_event(payload)

    assert event["type"] == "cl.radiobeacon.item.dispatched"
    assert event["source"] == "radiobeacon/log_handler"
    assert event["data"]["source"] == "senapred"
    assert event["data"]["item_id"] == "abc123"


def test_parse_cloud_event_accepts_bytes_payload():
    payload = json.dumps(
        {
            "specversion": "1.0",
            "id": "abc",
            "source": "radiobeacon/log_handler",
            "type": "cl.radiobeacon.item.dispatched",
            "data": {"source": "senapred", "item_id": "1"},
        }
    ).encode("utf-8")

    event = parse_cloud_event(payload)

    assert event["data"]["item_id"] == "1"


def test_build_cloud_event_payload_has_expected_envelope_fields():
    payload = build_cloud_event_payload(
        event_type="item.chunked",
        actor="actions.chunk",
        data={"source": "senapred", "item_id": "1", "chunk_index": 0, "text": "hello"},
    )

    body = json.loads(payload)
    assert body["type"] == "cl.radiobeacon.item.chunked"
    assert body["source"] == "radiobeacon/actions.chunk"
    assert body["specversion"] == "1.0"
    assert body["data"] == {
        "source": "senapred",
        "item_id": "1",
        "chunk_index": 0,
        "text": "hello",
    }
