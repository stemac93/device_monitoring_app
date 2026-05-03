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


def add_user(username: str, password: str) -> None:
    url = f"{_base_url()}/users"
    r = requests.post(
        url,
        json={"username": username, "password": password},
        headers=_headers(),
        timeout=15,
    )
    if not r.ok:
        raise MosquittoAdminError(f"add_user failed: {r.status_code} {r.text}")
    logger.info("Added MQTT user %s", username)


def delete_user(username: str) -> None:
    url = f"{_base_url()}/users/{username}"
    r = requests.delete(url, headers=_headers(), timeout=15)
    if not r.ok:
        raise MosquittoAdminError(f"delete_user failed: {r.status_code} {r.text}")
    logger.info("Removed MQTT user %s", username)


def rewrite_acl(entries: List[AclEntry]) -> None:
    url = f"{_base_url()}/acl"
    payload = {
        "entries": [{"username": e.username, "topics": e.topics} for e in entries]
    }
    r = requests.post(url, json=payload, headers=_headers(), timeout=15)
    if not r.ok:
        raise MosquittoAdminError(f"rewrite_acl failed: {r.status_code} {r.text}")
    logger.info("Rewrote ACL with %d entries", len(entries))


def reload_broker() -> None:
    url = f"{_base_url()}/reload"
    r = requests.post(url, headers=_headers(), timeout=15)
    if not r.ok:
        raise MosquittoAdminError(f"reload_broker failed: {r.status_code} {r.text}")
    logger.info("Mosquitto reloaded")


def health() -> dict:
    url = f"{_base_url()}/health"
    r = requests.get(url, timeout=5)
    if not r.ok:
        raise MosquittoAdminError(f"health failed: {r.status_code}")
    return r.json()
