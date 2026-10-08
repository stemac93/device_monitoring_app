"""
Client per parlare con il container mosquitto-admin.

Usato dai signal Django (post_save / post_delete sul Gateway) e dall'admin
action di download bundle.

Configurazione via env (vedi settings):
    MOSQUITTO_ADMIN_URL    es: http://mosquitto-admin:8080
    MOSQUITTO_ADMIN_TOKEN  token condiviso col helper
"""

import logging
import os
from dataclasses import dataclass
from typing import List

import requests

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return os.getenv("MOSQUITTO_ADMIN_URL", "http://mosquitto-admin:8080").rstrip("/")


def _headers() -> dict:
    token = os.getenv("MOSQUITTO_ADMIN_TOKEN", "")
    if not token:
        raise RuntimeError("MOSQUITTO_ADMIN_TOKEN non impostata in env")
    return {"Authorization": f"Bearer {token}"}


@dataclass
class AclEntry:
    username: str
    topics: List[str]  # ['r plants/#', 'rw plants/1/#'], etc.


class MosquittoAdminError(Exception):
    pass


def _request(method: str, path: str, **kwargs) -> requests.Response:
    """Chiamata autenticata a mosquitto-admin.

    Qualsiasi errore (token mancante, helper irraggiungibile, timeout, risposta
    non 2xx) diventa MosquittoAdminError, l'unica eccezione che i chiamanti
    (signal, admin action) gestiscono.
    """
    try:
        r = requests.request(method, f"{_base_url()}{path}", headers=_headers(), timeout=15, **kwargs)
    except (RuntimeError, requests.RequestException) as e:
        raise MosquittoAdminError(f"{method} {path} failed: {e}") from e
    if not r.ok:
        raise MosquittoAdminError(f"{method} {path} failed: {r.status_code} {r.text}")
    return r


def add_user(username: str, password: str) -> None:
    _request("POST", "/users", json={"username": username, "password": password})
    logger.info("Added MQTT user %s", username)


def delete_user(username: str) -> None:
    _request("DELETE", f"/users/{username}")
    logger.info("Removed MQTT user %s", username)


def rewrite_acl(entries: List[AclEntry]) -> None:
    payload = {
        "entries": [{"username": e.username, "topics": e.topics} for e in entries]
    }
    _request("POST", "/acl", json=payload)
    logger.info("Rewrote ACL with %d entries", len(entries))


def reload_broker() -> None:
    _request("POST", "/reload")
    logger.info("Mosquitto reloaded")


def health() -> dict:
    url = f"{_base_url()}/health"
    r = requests.get(url, timeout=5)
    if not r.ok:
        raise MosquittoAdminError(f"health failed: {r.status_code}")
    return r.json()
