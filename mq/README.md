# mq

Runs a self-hosted [Mosquitto](https://mosquitto.org) MQTT broker in
Docker — a local, no-cloud-dependency broker that
[dispatcher](../dispatcher/README.md) optionally publishes select events
to as [CloudEvents](https://cloudevents.io), meant for running the whole
pipeline offline on constrained hardware (e.g. a ZimaBoard). MQTT was
chosen over a heavier broker (e.g. RabbitMQ) for two reasons: CloudEvents
has an official MQTT protocol binding (unlike RabbitMQ's AMQP 0-9-1
dialect, which CloudEvents doesn't formally cover), and MQTT's footprint
fits this project's constrained-hardware target much better.

## Usage

```bash
./start.sh
```

Brings up the container (`docker compose up -d`) and waits for it to
accept connections — reads the same repo-root `.env` as every other
package (though this package's `docker-compose.yml` doesn't currently need
any values from it). The broker listens on `localhost:1883` only (see
`docker-compose.yml`) — not exposed to the network by default.
`mosquitto.conf` allows anonymous connections — safe here since the port
is loopback-only; see `dispatcher/README.md` if you want to layer on auth
later.

Then point dispatcher at it (see
[dispatcher/README.md](../dispatcher/README.md#publishing-to-a-message-queue-cloudevents-over-mqtt)):

```bash
DISPATCHER_MQ_HOST=localhost ./dispatcher/start.sh
```

## Inspecting the queue

Quickest check, no extra install — subscribe to everything from inside the
container:

```bash
docker compose exec mosquitto mosquitto_sub -h localhost -t 'radiobeacon/events/#' -v
```

Or, for programmatic consumption, a short `paho-mqtt` subscriber:

```python
import paho.mqtt.subscribe as subscribe

def on_message(client, userdata, message):
    print(message.topic, message.payload.decode())

subscribe.callback(on_message, "radiobeacon/events/#", hostname="localhost")
```

## Deploying on a ZimaBoard

ZimaBoard is x86_64, so the standard `eclipse-mosquitto` Docker image runs
as-is — no ARM-specific image or cross-compilation concerns, unlike a
Raspberry Pi. Docker + Docker Compose need to already be installed on the
board; everything else here is identical to running it on any other Linux
host.

## Stopping / removing

```bash
docker compose down          # stop the container, keep persisted state
docker compose down -v       # also delete the persistence volume
```
