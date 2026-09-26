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

from django.db import transaction
from django.db.models.signals import m2m_changed, post_save, post_delete
from django.dispatch import receiver

from .models import Gateway, Device, DeviceData, EnergyData, GatewayData, GatewayMqttCredentials
from .mqtt import admin_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal originale: propagazione utenti m2m (preservato dal codice esistente)
# ---------------------------------------------------------------------------

# Modelli la cui visibilità per utente deriva da Gateway.user (tutti hanno la
# FK `Gateway` e il M2M `user`)
USER_SCOPED_MODELS = (Device, DeviceData, EnergyData, GatewayData)
_BULK_BATCH = 5000


def _add_users(gateway_ids, user_ids):
    """Aggiunge gli utenti a tutte le righe dei gateway, lavorando direttamente
    sulla tabella di relazione (una riga per volta sarebbe N+1 su milioni di dati)."""
    if not gateway_ids or not user_ids:
        return
    for model in USER_SCOPED_MODELS:
        through = model.user.through
        fk = f"{model._meta.model_name}_id"
        batch = []
        obj_ids = model.objects.filter(Gateway_id__in=gateway_ids).values_list("pk", flat=True)
        for obj_id in obj_ids.iterator(chunk_size=_BULK_BATCH):
            batch.extend(through(**{fk: obj_id, "user_id": uid}) for uid in user_ids)
            if len(batch) >= _BULK_BATCH:
                through.objects.bulk_create(batch, ignore_conflicts=True)
                batch = []
        if batch:
            through.objects.bulk_create(batch, ignore_conflicts=True)


def _remove_users(gateway_ids, user_ids=None):
    """Toglie gli utenti (tutti se user_ids è None) dalle righe dei gateway."""
    if not gateway_ids:
        return
    for model in USER_SCOPED_MODELS:
        rows = model.user.through.objects.filter(
            **{f"{model._meta.model_name}__Gateway_id__in": gateway_ids}
        )
        if user_ids is not None:
            rows = rows.filter(user_id__in=user_ids)
        rows.delete()


@receiver(m2m_changed, sender=Gateway.user.through)
def sync_users_to_devices_and_data(sender, instance, action, reverse, pk_set, **kwargs):
    # reverse=True: modifica dal lato utente (user.user_gateway.add(gw))
    if reverse:
        gateway_ids, user_ids = list(pk_set or []), [instance.pk]
    else:
        gateway_ids, user_ids = [instance.pk], list(pk_set or [])

    if action == "post_add":
        _add_users(gateway_ids, user_ids)
    elif action == "post_remove":
        _remove_users(gateway_ids, user_ids)
    elif action == "pre_clear":
        # pk_set è None nel clear: i gateway coinvolti vanno letti prima
        if reverse:
            _remove_users(list(instance.user_gateway.values_list("pk", flat=True)), [instance.pk])
        else:
            _remove_users([instance.pk])


@receiver(post_save, sender=Device)
def device_inherits_gateway_users(sender, instance, raw, update_fields=None, **kwargs):
    """Un device nuovo o spostato vede gli utenti del suo gateway."""
    # update_fields: salvataggi parziali (es. availability dal task) non toccano il gateway
    if raw or update_fields is not None or instance.Gateway_id is None:
        return
    instance.user.set(instance.Gateway.user.all())


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


def _sync_mosquitto_state() -> bool:
    """Riscrive l'ACL e ricarica il broker. Ritorna False se fallisce."""
    try:
        entries = _build_acl_entries()
        admin_client.rewrite_acl(entries)
        admin_client.reload_broker()
        return True
    except admin_client.MosquittoAdminError as e:
        logger.error("Failed to sync Mosquitto state: %s", e)
        return False


def _provision_gateway_user(username: str, password: str) -> None:
    try:
        admin_client.add_user(username=username, password=password)
        _sync_mosquitto_state()
    except admin_client.MosquittoAdminError as e:
        logger.error(
            "MQTT provisioning failed for %s - DB record saved, broker NOT updated: %s",
            username,
            e,
        )


def _remove_gateway_user(username: str) -> None:
    try:
        admin_client.delete_user(username)
        _sync_mosquitto_state()
        logger.info("Removed MQTT user %s after gateway delete", username)
    except admin_client.MosquittoAdminError as e:
        logger.error("Failed to remove MQTT user %s: %s", username, e)


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

    # Il broker va toccato solo se la transazione del salvataggio va a buon fine
    transaction.on_commit(lambda: _provision_gateway_user(username, password))


@receiver(post_delete, sender=Gateway)
def gateway_post_delete(sender, instance, **kwargs):
    username = _gateway_username(instance.pk)
    transaction.on_commit(lambda: _remove_gateway_user(username))
