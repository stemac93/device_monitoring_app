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
    value: JSON con { "ts": epoch_s, "values": {"input:640": [int_value, epoch_s], ...} }

Un device può avere più blocchi di lettura (input e holding): Telegraf può
pubblicarli in messaggi separati sullo stesso topic. Per questo put_raw fa il
merge con lo snapshot esistente e ogni registro ha il proprio timestamp, così
get_raw scarta solo i registri non più aggiornati.
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
_PROCESSED_PREFIX = "mqtt:processed:"


def _key(device_pk: int) -> str:
    return f"{_KEY_PREFIX}{device_pk}"


def _encode(key) -> str:
    register_type, addr = key
    return f"{register_type}:{addr}"


def _decode(key: str):
    register_type, addr = key.split(":", 1)
    return register_type, int(addr)


def put_raw(device_pk: int, base_values: dict, ts: Optional[float] = None) -> None:
    """Aggiunge/aggiorna i registri grezzi di un device nello snapshot in cache.

    base_values: dict {(register_type, int_address): int_raw_register_value}
    """
    ts = ts if ts is not None else time.time()
    values = {}
    raw = _redis.get(_key(device_pk))
    if raw:
        try:
            values = json.loads(raw).get("values", {})
        except json.JSONDecodeError:
            values = {}
    for key, val in base_values.items():
        values[_encode(key)] = [val, ts]
    payload = {"ts": max([ts] + [v[1] for v in values.values()]), "values": values}
    _redis.set(_key(device_pk), json.dumps(payload), ex=RAW_TTL_SECONDS)


def _get_fresh(device_pk: int, max_age_seconds: Optional[int] = None):
    """Ritorna (ts_snapshot, {(register_type, addr): valore}) con i soli registri
    più recenti della soglia di freschezza, oppure None."""
    raw = _redis.get(_key(device_pk))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    age_limit = max_age_seconds if max_age_seconds is not None else RAW_FRESHNESS_SECONDS
    now = time.time()
    fresh = {
        _decode(k): int(v[0])
        for k, v in payload.get("values", {}).items()
        if now - float(v[1]) <= age_limit
    }
    if not fresh:
        return None
    return payload.get("ts"), fresh


def get_raw(device_pk: int, max_age_seconds: Optional[int] = None) -> Optional[dict]:
    """Recupera l'ultimo snapshot grezzo per un device.

    Ritorna None se assente o se nessun registro è abbastanza recente.
    Ritorna dict {(register_type, int_address): int_value} altrimenti,
    con i soli registri più recenti della soglia di freschezza.
    """
    fresh = _get_fresh(device_pk, max_age_seconds)
    return fresh[1] if fresh is not None else None


def claim_raw(device_pk: int) -> Optional[dict]:
    """Come get_raw, ma ogni snapshot viene restituito UNA sola volta.

    Se il gateway smette di pubblicare (o Telegraf è più lento del beat) il task
    ritroverebbe lo stesso snapshot a ogni ciclo e lo salverebbe più volte,
    gonfiando disponibilità ed energia. Il ts dell'ultimo snapshot elaborato
    è memorizzato in Redis.
    """
    fresh = _get_fresh(device_pk)
    if fresh is None:
        return None
    ts, values = fresh
    ts = str(ts)
    processed_key = f"{_PROCESSED_PREFIX}{device_pk}"
    if _redis.get(processed_key) == ts:
        return None
    _redis.set(processed_key, ts, ex=RAW_TTL_SECONDS)
    return values


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
