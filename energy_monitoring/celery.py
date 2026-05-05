import os
from celery import Celery
from celery.schedules import crontab
from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "energy_monitoring.settings")
app = Celery("energy_monitoring")
app.config_from_object("django.conf:settings", namespace="CELERY")

# Nota architettura post-migrazione MQTT:
# - 'check_devices': non fa più polling Modbus TCP, ma processa gli ultimi
#   valori RAW pubblicati da Telegraf (via consumer MQTT che popola la cache
#   Redis). Tienilo allineato o leggermente più lungo dell'intervallo Telegraf
#   (impostato in export_telegraf_config, default 30s).
# - 'compute_plant_metrics': invariato, opera solo su dati già persistiti in DB.
# - 'midnight_energy_aggregation': invariato.

app.conf.beat_schedule = {
    "check_devices": {
        "task": "user_devices.tasks.check_all_devices",
        "schedule": settings.CELERY_BEAT_SCHEDULE_INTERVAL,
    },
    "compute_plant_metrics": {
        "task": "user_devices.tasks.compute_plant_metrics",
        "schedule": settings.CELERY_PLANT_METRICS_INTERVAL,
    },
    "midnight_energy_aggregation": {
        "task": "user_devices.tasks.midnight_energy_aggregation",
        "schedule": crontab(hour=0, minute=5),
    },
}

app.autodiscover_tasks()
