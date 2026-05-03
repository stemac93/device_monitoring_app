"""
Signal Django per user_devices.

Mantiene:
- m2m_changed sul Gateway.user → propaga gli utenti ai Device e DeviceData
- post_save/post_delete sul Gateway → mantiene Mosquitto sincronizzato

I signal MQTT sono best-effort: se mosquitto-admin non risponde, logga ma
non blocca la transazione DB.
"""

import logging
import secrets

from django.db.models.signals import m2m_changed, post_save, post_delete
from django.dispatch import receiver

from .models import Gateway, Device, DeviceData, GatewayMqttCredentials
from .mqtt import admin_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal originale: propagazione utenti m2m (preservato dal codice esistente)
# ---------------------------------------------------------------------------

@receiver(m2m_changed, sender=Gateway.user.through)
def sync_users_to_devices_and_data(sender, instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        users = instance.user.all()
        for device in instance.devices.all():
            device.user.set(users)
            for data in device.device_data.all():
                data.user.set(users)


# ---------------------------------------------------------------------------
# Provisioning MQTT automatico per Gateway
# ---------------------------------------------------------------------------

def _gateway_username(gw_pk: int) -> str:
    return f"gw-{gw_pk}"


def _generate_password() -> str:
    return secrets.token_urlsafe(32)


def _build_acl_entries():
    entries = [
        admin_client.AclEntry(username="django-consumer", topics=["r plants/#"]),
    ]
    for cred in GatewayMqttCredentials.objects.select_related("gateway").all():
        entries.append(
            admin_client.AclEntry(
                username=cred.username,
                topics=[f"rw plants/{cred.gateway_id}/#"],
            )
        )
    return entries


def _sync_mosquitto_state():
    try:
        entries = _build_acl_entries()
        admin_client.rewrite_acl(entries)
        admin_client.reload_broker()
    except admin_client.MosquittoAdminError as e:
        logger.error("Failed to sync Mosquitto state: %s", e)


@receiver(post_save, sender=Gateway)
def gateway_post_save(sender, instance, created, raw, **kwargs):
    if raw:
        return
    if not created:
        return
    if GatewayMqttCredentials.objects.filter(gateway=instance).exists():
        return

    username = _gateway_username(instance.pk)
    password = _generate_password()

    GatewayMqttCredentials.objects.create(
        gateway=instance,
        username=username,
        password_plaintext=password,
        password_revealed=False,
    )
    logger.info(
        "Created MQTT credentials for gateway pk=%s username=%s",
        instance.pk,
        username,
    )

    try:
        admin_client.add_user(username=username, password=password)
        _sync_mosquitto_state()
    except admin_client.MosquittoAdminError as e:
        logger.error(
            "MQTT provisioning failed for %s - DB record saved, broker NOT updated: %s",
            username,
            e,
        )


@receiver(post_delete, sender=Gateway)
def gateway_post_delete(sender, instance, **kwargs):
    username = _gateway_username(instance.pk)
    try:
        admin_client.delete_user(username)
        _sync_mosquitto_state()
        logger.info("Removed MQTT user %s after gateway delete", username)
    except admin_client.MosquittoAdminError as e:
        logger.error("Failed to remove MQTT user %s: %s", username, e)
