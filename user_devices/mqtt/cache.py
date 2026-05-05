"""
Cache Redis per ultimi valori RAW letti da MQTT.

Architettura scelta:
- Il consumer MQTT long-running riceve messaggi pubblicati da Telegraf ogni ~30s
  e li mette in cache Redis con TTL. NON scrive direttamente nel DB.
- Il task Celery periodico (scan_and_read_devices) legge la cache, applica
  map_variables/compute_variables/compute_energy esattamente come fa oggi
  dopo il polling Modbus, e scrive in DB.

Questo disaccoppia il ritmo di arrivo dati dal ritmo di persistenza/aggregazione,
ed è robusto a riavvii del consumer (le retained message sul broker ripopolano
la cache, oppure il ciclo successivo Telegraf ripubblica).

Formato cache:
    key: "mqtt:rawdata:{device_pk}"
    value: JSON con { "ts": epoch_s, "base_values": {int_addr: int_value, ...} }
"""

import json
import time
from typing import Optional

from redis import Redis

# Stesso host/porta Redis usati da Celery (cfr. settings.py CELERY_BROKER_URL)
_redis = Redis(host="redis", port=6379, decode_responses=True)

# TTL: se non arriva aggiornamento entro questo intervallo, il valore viene
# considerato stale. Impostato molto più alto dell'intervallo Telegraf per
# tollerare downtime broker/gateway senza perdere subito lo stato.
RAW_TTL_SECONDS = 15 * 60  # 15 minuti

# Soglia di freschezza: il task Celery scarta valori più vecchi di questa soglia
# per non pubblicare dati finti in caso di disconnessione prolungata del gateway.
# Con Telegraf a 30s e beat a 60s, 5 minuti lascia margine a riavvii del consumer.
RAW_FRESHNESS_SECONDS = 5 * 60

_KEY_PREFIX = "mqtt:rawdata:"


def _key(device_pk: int) -> str:
    return f"{_KEY_PREFIX}{device_pk}"


def put_raw(device_pk: int, base_values: dict, ts: Optional[float] = None) -> None:
    """Memorizza l'ultimo snapshot di registri grezzi per un device.

    base_values: dict {int_address: int_raw_register_value}
    """
    payload = {
        "ts": ts if ts is not None else time.time(),
        # Redis serializza JSON, le chiavi dict vanno a stringa: teniamolo esplicito
        "base_values": {str(addr): val for addr, val in base_values.items()},
    }
    _redis.set(_key(device_pk), json.dumps(payload), ex=RAW_TTL_SECONDS)


def get_raw(device_pk: int, max_age_seconds: Optional[int] = None) -> Optional[dict]:
    """Recupera l'ultimo snapshot grezzo per un device.

    Ritorna None se assente o troppo vecchio.
    Ritorna dict {int_address: int_value} altrimenti.
    """
    raw = _redis.get(_key(device_pk))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    age_limit = max_age_seconds if max_age_seconds is not None else RAW_FRESHNESS_SECONDS
    if time.time() - float(payload.get("ts", 0)) > age_limit:
        return None

    # Ricostruisci int: addr, Telegraf ha pubblicato in int, li abbiamo stringified in put_raw
    return {int(k): int(v) for k, v in payload.get("base_values", {}).items()}


def get_raw_age(device_pk: int) -> Optional[float]:
    """Età in secondi dell'ultimo snapshot, o None se assente."""
    raw = _redis.get(_key(device_pk))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return time.time() - float(payload.get("ts", 0))
    except (json.JSONDecodeError, ValueError):
        return None
