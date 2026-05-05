"""
Consumer MQTT long-running.

Sottoscrive i topic pubblicati dai Telegraf sui gateway e aggiorna la cache
Redis con gli ultimi valori RAW. Non tocca il DB Django: la persistenza è a
carico del task Celery `scan_and_read_devices` che legge la cache.

Topic sottoscritti:
    plants/+/devices/+/raw       → dati Modbus grezzi
    plants/+/status              → LWT gateway (online/offline)
    plants/+/devices/+/status    → (opzionale) LWT per-device

Payload atteso su .../raw (Telegraf `output.mqtt_v2` con `data_format = "json"`):
    {
      "name": "modbus",
      "timestamp": 1745161800,
      "tags": {
        "gateway_id": "1",
        "device_id": "42",
        "slave_id": "1"
      },
      "fields": {
        "reg_0x0280": 2301,
        "reg_0x0281": 15
      }
    }

Lanciabile come:
    python -m user_devices.mqtt.consumer

Variabili ambiente:
    MQTT_BROKER_HOST    (default: mosquitto)
    MQTT_BROKER_PORT    (default: 8883 se MQTT_TLS=1 altrimenti 1883)
    MQTT_USERNAME
    MQTT_PASSWORD
    MQTT_TLS            (1/0, default 1)
    MQTT_TLS_CA         (default: /certs/ca.crt)
    MQTT_TLS_INSECURE   (1/0, default 0; metti 1 solo per self-signed senza SAN)
    MQTT_CLIENT_ID      (default: django-consumer)
"""

import json
import logging
import os
import signal
import sys
import time

import django
import paho.mqtt.client as mqtt

# Bootstrap Django perché questo script gira come processo separato
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "energy_monitoring.settings")
django.setup()

from user_devices.mqtt.cache import put_raw  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("MQTT_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

TOPIC_RAW = "plants/+/devices/+/raw"
TOPIC_GW_STATUS = "plants/+/status"
TOPIC_DEV_STATUS = "plants/+/devices/+/status"


def _parse_register_field(field_name: str):
    """Da 'reg_0x0280' restituisce 0x280 (int). Ritorna None se il formato non combacia."""
    if not field_name.startswith("reg_"):
        return None
    try:
        return int(field_name[4:], 16)
    except ValueError:
        return None


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("Connected to MQTT broker")
        client.subscribe([(TOPIC_RAW, 1), (TOPIC_GW_STATUS, 1), (TOPIC_DEV_STATUS, 1)])
    else:
        logger.error("MQTT connection failed, rc=%s", rc)


def on_disconnect(client, userdata, rc, properties=None):
    logger.warning("MQTT disconnected rc=%s - paho auto-reconnect in loop_forever()", rc)


def on_message(client, userdata, msg):
    try:
        _dispatch(msg.topic, msg.payload)
    except Exception:
        # Non rilanciare: un messaggio malformato non deve uccidere il consumer
        logger.exception("Error handling message on %s", msg.topic)


def _dispatch(topic: str, payload: bytes) -> None:
    parts = topic.split("/")
    # plants/{gw}/devices/{dev}/raw → 5 parti
    # plants/{gw}/status → 3 parti
    # plants/{gw}/devices/{dev}/status → 5 parti

    if len(parts) == 5 and parts[0] == "plants" and parts[2] == "devices" and parts[4] == "raw":
        _handle_raw(parts[1], parts[3], payload)
    elif len(parts) == 3 and parts[0] == "plants" and parts[2] == "status":
        logger.info("Gateway %s status: %s", parts[1], payload.decode(errors="replace"))
    elif len(parts) == 5 and parts[4] == "status":
        logger.info("Device %s/%s status: %s", parts[1], parts[3], payload.decode(errors="replace"))
    else:
        logger.debug("Unmatched topic: %s", topic)


def _handle_raw(gateway_topic_id: str, device_topic_id: str, payload: bytes) -> None:
    if not payload:
        # payload vuoto = retained clear, ignoriamo
        return

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Invalid JSON on plants/%s/devices/%s/raw", gateway_topic_id, device_topic_id)
        return

    fields = data.get("fields") or {}
    tags = data.get("tags") or {}

    # device_id dai tag è la source of truth (topic può essere mascherato da wildcard)
    try:
        device_pk = int(tags.get("device_id", device_topic_id))
    except (TypeError, ValueError):
        logger.warning("Cannot parse device_id tag in message: %s", tags)
        return

    base_values = {}
    for fname, fvalue in fields.items():
        addr = _parse_register_field(fname)
        if addr is None:
            continue
        try:
            base_values[addr] = int(fvalue)
        except (TypeError, ValueError):
            continue

    if not base_values:
        logger.debug("No register fields in message for device %s", device_pk)
        return

    ts = data.get("timestamp")
    try:
        ts = float(ts) if ts is not None else None
    except (TypeError, ValueError):
        ts = None

    put_raw(device_pk, base_values, ts=ts)
    logger.debug(
        "Cached %d registers for device_pk=%s (ts=%s, first=0x%04x)",
        len(base_values),
        device_pk,
        ts,
        min(base_values),
    )


def build_client() -> mqtt.Client:
    client_id = os.getenv("MQTT_CLIENT_ID", "django-consumer")
    client = mqtt.Client(
        client_id=client_id,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        protocol=mqtt.MQTTv5,
        clean_session=None,
    )

    user = os.getenv("MQTT_USERNAME")
    pwd = os.getenv("MQTT_PASSWORD")
    if user:
        client.username_pw_set(user, pwd or None)

    use_tls = os.getenv("MQTT_TLS", "1") not in ("0", "false", "False", "")
    if use_tls:
        ca = os.getenv("MQTT_TLS_CA", "/certs/ca.crt")
        client.tls_set(ca_certs=ca if os.path.exists(ca) else None)
        if os.getenv("MQTT_TLS_INSECURE", "0") in ("1", "true", "True"):
            client.tls_insecure_set(True)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # Reconnect esponenziale, paho gestisce da solo in loop_forever
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def main() -> int:
    host = os.getenv("MQTT_BROKER_HOST", "mosquitto")
    use_tls = os.getenv("MQTT_TLS", "1") not in ("0", "false", "False", "")
    port = int(os.getenv("MQTT_BROKER_PORT", "8883" if use_tls else "1883"))

    client = build_client()

    def _shutdown(signum, frame):
        logger.info("Received signal %s, shutting down", signum)
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        try:
            logger.info("Connecting to %s:%s (tls=%s)", host, port, use_tls)
            client.connect(host, port, keepalive=60)
            client.loop_forever(retry_first_connection=True)
        except Exception:
            logger.exception("Consumer crashed, restarting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
