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
    """Stato ACL: consumer + tutti i gateway in modalità MQTT.

    I gateway in modbus_direct/dlms restano nel DB (e le loro credenziali
    eventualmente esistenti pure), ma NON sono inclusi nell'ACL — quindi se
    qualcuno tenta di pubblicare con quelle credenziali viene rifiutato.
    """
    entries = [
        admin_client.AclEntry(username="django-consumer", topics=["r plants/#"]),
    ]
    for cred in GatewayMqttCredentials.objects.select_related("gateway").all():
        if cred.gateway.protocol_mode != "mqtt":
            continue
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
    """Genera/elimina credenziali MQTT in base a `protocol_mode`.

    - Alla creazione di un gateway con protocol_mode='mqtt': genera password
      e configura Mosquitto.
    - Cambio di un gateway esistente da/verso 'mqtt': aggiorna provisioning.
      Se passa a non-mqtt, le credenziali rimangono nel DB ma l'utente viene
      rimosso dal broker (per evitare connessioni stale).
    - Cambio per altri campi (nome, ip, ecc.): nessuna azione MQTT.
    """
    if raw:
        return

    has_creds = GatewayMqttCredentials.objects.filter(gateway=instance).exists()
    is_mqtt = instance.protocol_mode == "mqtt"

    if created:
        # Nuovo gateway
        if not is_mqtt:
            return  # niente provisioning per modalità non-mqtt
        _provision_mqtt_credentials(instance)
        return

    # Aggiornamento di un gateway esistente
    if is_mqtt and not has_creds:
        # È stato cambiato a mqtt, va provisionato
        _provision_mqtt_credentials(instance)
    elif not is_mqtt and has_creds:
        # È stato cambiato a non-mqtt: rimuovo l'utente dal broker
        # ma lascio il record DB (nel caso si torni a mqtt)
        username = _gateway_username(instance.pk)
        try:
            admin_client.delete_user(username)
            _sync_mosquitto_state()
            logger.info(
                "Gateway %s passato a %s, utente MQTT %s rimosso dal broker",
                instance.pk,
                instance.protocol_mode,
                username,
            )
        except admin_client.MosquittoAdminError as e:
            logger.error("Failed to remove MQTT user %s after mode change: %s", username, e)


def _provision_mqtt_credentials(instance):
    """Genera credenziali MQTT per un Gateway e configura Mosquitto."""
    username = _gateway_username(instance.pk)
    password = _generate_password()

    GatewayMqttCredentials.objects.update_or_create(
        gateway=instance,
        defaults={
            "username": username,
            "password_plaintext": password,
            "password_revealed": False,
        },
    )
    logger.info(
        "Provisioned MQTT credentials for gateway pk=%s username=%s",
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
    """Rimuove utente Mosquitto se esisteva (indipendente dal protocol_mode)."""
    username = _gateway_username(instance.pk)
    try:
        admin_client.delete_user(username)
        _sync_mosquitto_state()
        logger.info("Removed MQTT user %s after gateway delete", username)
    except admin_client.MosquittoAdminError as e:
        logger.error("Failed to remove MQTT user %s: %s", username, e)
