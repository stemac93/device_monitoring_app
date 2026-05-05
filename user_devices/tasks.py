import logging
import time

from celery import shared_task, group
from pymodbus.client import ModbusTcpClient
from redis import Redis
from redis.lock import Lock

from .models import Device, Gateway, DeviceData, EnergyData
import user_devices.functions as functions
from .mqtt.cache import get_raw, get_raw_age
from .helper_funcs import convert_to_local_time

logger = logging.getLogger(__name__)

redis_client = Redis(host="redis", port=6379)

"""
Celery task, un'istanza per gateway. Comportamento per ramo Modbus:

- Se gateway.use_mqtt == False (default): polling Modbus TCP diretto via mbusd,
  come prima della migrazione. Nessun cambiamento osservabile.
- Se gateway.use_mqtt == True: legge gli ultimi valori RAW dalla cache Redis
  popolata dal consumer MQTT (Telegraf pubblica ogni ~30s).

Il ramo DLMS è invariato e non dipende dal flag.

Questo permette una migrazione gateway-per-gateway: abiliti `use_mqtt` su un
singolo Gateway dopo aver configurato Telegraf sul suo RPi, e solo quel
gateway passa al nuovo percorso. Gli altri continuano con polling TCP.
"""


@shared_task(soft_time_limit=240, time_limit=300)
def scan_and_read_devices(gateway_ip):
    lock_key = f"lock_device_{gateway_ip}"

    with Lock(redis_client, lock_key, timeout=300):
        try:
            gateway = Gateway.objects.get(ip_address=gateway_ip)
        except Gateway.DoesNotExist:
            logger.warning("Gateway %s not found", gateway_ip)
            return

        logger.info(
            "Processing devices for gateway %s (use_mqtt=%s)",
            gateway.ip_address,
            gateway.use_mqtt,
        )
        devices = Device.objects.filter(Gateway=gateway)
        if not devices:
            logger.info("No devices found for gateway %s", gateway.ip_address)
            return

        skip_dlms_devices = False
        client = None

        for device in devices:
            if not device.is_enabled:
                continue

            values = None
            try:
                if device.protocol == "modbus":
                    if gateway.use_mqtt:
                        values = _process_modbus_from_cache(device)
                    else:
                        client, values = _process_modbus_from_tcp(device, gateway, client)

                elif device.protocol == "dlms" and not skip_dlms_devices:
                    # Ramo DLMS: invariato, polling diretto
                    if not functions.probe_dlms_device(device, timeout=30):
                        skip_dlms_devices = True
                        time.sleep(1)
                        continue
                    logger.info("Reading DLMS device %s", device.name)
                    values = functions.read_dlms_values(device)
                    logger.info("DLMS values read: %s", values)
                else:
                    continue

                if values is not None:
                    device.availability = functions.compute_device_availability(device, values)
                    logger.info("Device availability: %s", device.availability)

                    device_data = DeviceData.objects.filter(device_name=device)
                    energy_data = EnergyData.objects.filter(device_name=device)
                    energy_values = functions.compute_energy(values, device_data, energy_data)

                    functions.store_data_in_database(device, values)
                    logger.info("Data saved for device %s", device.name)

                    if energy_values is not None:
                        functions.store_energy_data_in_database(device, energy_values)
                        device.daily_production = energy_values.get(
                            "Energy_daily_produced", {}
                        ).get("value", 0.0)
                        device.daily_consumption = energy_values.get(
                            "Energy_daily_consumed", {}
                        ).get("value", 0.0)
                        logger.info("Energy data saved for device %s", device.name)

                    device.save()

            except Exception as e:
                logger.error("Error processing device %s: %s", device.name, e)
                continue
            finally:
                time.sleep(0.1)

        # Chiudi eventuale client TCP aperto in modalità polling
        if client is not None:
            try:
                client.close()
            except Exception as e:
                logger.warning("Error closing Modbus client: %s", e)


def _process_modbus_from_cache(device):
    """Modalità MQTT: recupera ultimi registri RAW dalla cache Redis popolata
    dal consumer MQTT.

    Ritorna dict `values` nello stesso formato del polling, oppure None se
    dati assenti o stale (in quel caso il device non viene aggiornato in DB
    per evitare di scrivere valori finti durante disconnessioni).
    """
    base_values = get_raw(device.pk)
    if base_values is None:
        age = get_raw_age(device.pk)
        if age is None:
            logger.info("No MQTT data yet for device %s (pk=%s)", device.name, device.pk)
        else:
            logger.warning(
                "Stale MQTT data for device %s (pk=%s, age=%.0fs) - skipping",
                device.name,
                device.pk,
                age,
            )
        return None

    logger.info(
        "MQTT cache hit for device %s (pk=%s): %d registers",
        device.name,
        device.pk,
        len(base_values),
    )

    mapped_values = functions.map_variables(base_values, device)
    computed_values = functions.compute_variables(mapped_values, device)
    return {**mapped_values, **computed_values}


def _process_modbus_from_tcp(device, gateway, client):
    """Modalità legacy: polling Modbus TCP diretto via mbusd, come prima.

    Ritorna (client, values) per riutilizzare il client tra device sullo
    stesso gateway quando possibile (anche se il codice originale chiudeva
    e riapriva per ogni device; qui ricalchiamo lo stesso pattern).
    """
    # Come nel codice originale: una nuova connessione per device
    if client is not None:
        try:
            client.close()
        except Exception:
            pass

    client = ModbusTcpClient(
        gateway.ip_address,
        port=device.port,
        timeout=30,
    )
    if not client.connect():
        logger.warning(
            "Failed to connect to device on %s:%s",
            gateway.ip_address,
            device.port,
        )
        client.close()
        time.sleep(1)
        return None, None

    logger.info("TCP connected to %s on %s:%s", device.name, gateway.ip_address, device.port)

    base_values = functions.read_modbus_registers(device, client)
    if base_values is None:
        return client, None

    mapped_values = functions.map_variables(base_values, device)
    computed_values = functions.compute_variables(mapped_values, device)
    return client, {**mapped_values, **computed_values}


@shared_task
def compute_plant_metrics():
    """Invariato: aggregazione per-gateway, opera solo su DB."""
    logger.info("Computing plant metrics for all gateways...")
    try:
        gateways = Gateway.objects.all()
        for gateway in gateways:
            try:
                devices = Device.objects.filter(Gateway=gateway)
                if not devices:
                    logger.info("No devices found for gateway %s", gateway.ip_address)
                    continue

                availability = functions.compute_plant_availability(gateway, devices)
                performance = functions.compute_plant_performance(gateway, devices)
                production = functions.compute_plant_production(gateway, devices)
                radiance_value = functions.find_radiance_value(devices)

                gateway_data = {
                    "availability": {"value": availability, "unit": "%"},
                    "performance": {"value": performance, "unit": "%"},
                    "production": {"value": production, "unit": "kW"},
                    "radiance": {"value": radiance_value, "unit": "W/m²"},
                }
                functions.store_gateway_data_in_database(gateway, gateway_data)
                logger.info("Plant metrics saved for gateway %s", gateway.ip_address)
            except Exception as e:
                logger.error("Error computing metrics for gateway %s: %s", gateway.ip_address, e)
                continue
    except Exception as e:
        logger.error("Error in compute_plant_metrics task: %s", e)


@shared_task
def check_all_devices():
    logger.info("Checking all devices...")
    gateways = Gateway.objects.all()
    gateway_ip = [gateway.ip_address for gateway in gateways]
    job = group(scan_and_read_devices.s(ip_address) for ip_address in gateway_ip)
    job.apply_async()


@shared_task
def midnight_energy_aggregation():
    """Invariato: legge solo dal DB."""
    from datetime import datetime, timezone, timedelta

    logger.info("Starting midnight energy aggregation...")
    try:
        now = datetime.now(timezone.utc)
        now_local = convert_to_local_time(now)
        today = now_local.date()

        gateways = Gateway.objects.all()
        if not gateways.exists():
            logger.info("No gateways found for energy aggregation")
            return

        start_of_day_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_day_local = convert_to_local_time(start_of_day_utc)
        start_of_day_filter = start_of_day_local.astimezone(timezone.utc)

        start_of_week_utc = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=now.weekday()
        )
        start_of_week_local = convert_to_local_time(start_of_week_utc)
        start_of_week_filter = start_of_week_local.astimezone(timezone.utc)

        start_of_month_utc = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_of_month_local = convert_to_local_time(start_of_month_utc)
        start_of_month_filter = start_of_month_local.astimezone(timezone.utc)

        for gateway in gateways:
            try:
                devices = Device.objects.filter(Gateway=gateway, is_enabled=True)
                if not devices.exists():
                    logger.info("No active devices for gateway %s", gateway.name)
                    continue

                aggregation_data = {
                    "data_type": "Data Aggregate",
                    "date": today.isoformat(),
                    "daily": {"produced": 0.0, "consumed": 0.0},
                    "weekly": {"produced": 0.0, "consumed": 0.0},
                    "monthly": {"produced": 0.0, "consumed": 0.0},
                }

                for device in devices:
                    try:
                        energy_data_queryset = EnergyData.objects.filter(device_name=device)
                        if not energy_data_queryset.exists():
                            continue

                        daily_data = energy_data_queryset.filter(timestamp__gte=start_of_day_filter)
                        weekly_data = energy_data_queryset.filter(timestamp__gte=start_of_week_filter)
                        monthly_data = energy_data_queryset.filter(timestamp__gte=start_of_month_filter)

                        def _sum(qs):
                            prod, cons = 0.0, 0.0
                            for record in qs:
                                data = record.data
                                if not isinstance(data, dict):
                                    continue
                                ep = data.get("Energy_produced", {})
                                ec = data.get("Energy_consumed", {})
                                if isinstance(ep, dict):
                                    prod += ep.get("value", 0.0)
                                if isinstance(ec, dict):
                                    cons += ec.get("value", 0.0)
                            return prod, cons

                        dp, dc = _sum(daily_data)
                        wp, wc = _sum(weekly_data)
                        mp, mc = _sum(monthly_data)

                        aggregation_data["daily"]["produced"] += dp
                        aggregation_data["daily"]["consumed"] += dc
                        aggregation_data["weekly"]["produced"] += wp
                        aggregation_data["weekly"]["consumed"] += wc
                        aggregation_data["monthly"]["produced"] += mp
                        aggregation_data["monthly"]["consumed"] += mc

                    except Exception as e:
                        logger.error("Error on device %s: %s", device.name, e)
                        continue

                for period in ("daily", "weekly", "monthly"):
                    aggregation_data[period]["produced"] = round(aggregation_data[period]["produced"], 2)
                    aggregation_data[period]["consumed"] = round(aggregation_data[period]["consumed"], 2)

                try:
                    aggregation_device_name = f"Aggregate_{gateway.name}"
                    aggregation_device, created = Device.objects.get_or_create(
                        name=aggregation_device_name,
                        defaults={
                            "Gateway": gateway,
                            "is_enabled": False,
                            "protocol": "modbus",
                        },
                    )
                    if created:
                        aggregation_device.user.set(gateway.user.all())

                    energy_data = EnergyData.objects.create(
                        Gateway=gateway,
                        device_name=aggregation_device,
                        data=aggregation_data,
                    )
                    energy_data.user.set(gateway.user.all())
                    logger.info("Gateway %s aggregation saved", gateway.name)
                except Exception as e:
                    logger.error("Error saving aggregation for %s: %s", gateway.name, e)
                    continue

            except Exception as e:
                logger.error("Error on gateway %s: %s", gateway.name, e)
                continue

        logger.info("Midnight energy aggregation completed")

    except Exception as e:
        logger.error("Error in midnight_energy_aggregation: %s", e)
