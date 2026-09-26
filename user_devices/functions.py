import logging
import time
import json
import requests
import math
from sympy import sympify
from datetime import datetime, timezone, timedelta
from pymodbus.client import ModbusTcpClient
from .models import ModbusMappingVariable, ComputedVariable, DeviceData, EnergyData, GatewayData
from decimal import Decimal
from django.db.models import Sum
from django.db.models.expressions import RawSQL
from fractions import Fraction
from .helper_funcs import sanitize_variable_name, convert_value, convert_to_local_time, round_to_2_decimals, local_period_starts
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_WORDS_PER_READ = 12
TIMEOUT = 5                 # Timeout per la connessione
# Oltre questo intervallo tra due letture l'energia non viene integrata
MAX_INTEGRATION_GAP_SECONDS = 10 * 60

"""
Probes a DLMS device to check if it is reachable.
"""
def probe_dlms_device(device, timeout=30):
    try:
        gateway_ip = device.Gateway.ip_address
        gateway_port = device.port

        rest_api_call = f"http://{gateway_ip}:{gateway_port}/dlms/clock"
        response = requests.get(rest_api_call, timeout=timeout)
        if response.ok:
            logger.info(f"DLMS device is reachable")
            return True
        else:
            logger.error(f"DLMS device is not reachable")
            return False    
    except Exception as e:
        logger.error(f"Error probing DLMS device: {e}")
        return False
        
"""
Reads DLMS registers for a given device.
"""
def read_dlms_values(device):

    mapped_values = {}
    gateway_ip = device.Gateway.ip_address
    gateway_port = device.port
    
    dlms_mappings = device.dlms_variables.all()

    ############################################################
    # MODIFICA TEMPORANEA PER LEGGERE DATI DAL CLIENTE ATTUALE #
    ############################################################
    # Aggregate the mappings with the same obis_code
    aggregated_mappings = {}
    for mapping in dlms_mappings:
        if mapping.obis_code in aggregated_mappings:
            aggregated_mappings[mapping.obis_code].append(mapping)
        else:
            aggregated_mappings[mapping.obis_code] = [mapping]
    logger.info(f"Aggregated mappings: {aggregated_mappings}")
    
    for obis in aggregated_mappings:
        try:

            # Aggregate the column_idx for the same obis_code
            rest_api_call = f"http://{gateway_ip}:{gateway_port}/dlms/profile"
            params = {"obis_code": obis}
            payload = []
            for mapping in aggregated_mappings[obis]:
                payload.append({
                    "varname": mapping.var_name,
                    "column": mapping.column_idx,
                    "conversion_factor": mapping.conversion_factor,
                    "unit": mapping.unit
                })          
            logger.info(f"Rest API call: {rest_api_call}")
            logger.info(f"Payload: {payload}")
            logger.info(f"Params: {params}")
            # Timeout: senza, un gateway bloccato terrebbe il task (e il lock) fino al kill
            response = requests.post(rest_api_call, params=params, json=payload, timeout=30)

            if response.ok:
                data = response.json()
                logger.info(f"Data: {data}")

                # Aggregate the values and the timestamps for each column_idx
                # (mapped_values accumula tutti gli OBIS: non va azzerato qui)
                for reading in data['results']:
                    sanitized_name = sanitize_variable_name(reading['varname'])
                    mapped_values[sanitized_name] = {
                        "value": round_to_2_decimals(reading['value']),
                        "unit": reading['unit']
                    }
                mapped_values['timestamp'] = data['timestamp']

                logger.info(f"Mapped values: {mapped_values}")
                logger.info(f"Last reading time: {mapped_values['timestamp']}")
            else:
                logger.error(f"Failed to get profile data: {response.status_code}")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error while reading DLMS values: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON response: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in read_dlms_values: {e}")
            return None
          
    json_result = json.dumps(mapped_values, indent=4)
    logger.info(f"Mapped JSON: {json_result}")
    return mapped_values


"""
Reads Modbus registers for a given device.
Handles multiple reads if needed due to word limits.
"""
def read_modbus_registers(device, client):
    try:
        start_address = int(device.start_address, 16)
        logger.info(f"Start Address: {start_address}")
        word_count = device.word_count 
        logger.info(f"Word count: {word_count}")
        
        # Split reads into chunks of MAX_WORDS_PER_READ
        base_values = {}
        for offset in range(0, word_count, MAX_WORDS_PER_READ):
            current_address = start_address + offset
            logger.info(f"Start Address: {current_address}")
            words_to_read = min(MAX_WORDS_PER_READ, word_count - offset)
            if hasattr(device, 'register_type') and device.register_type == 'holding':
                response = client.read_holding_registers(address=current_address, count=words_to_read, device_id=device.slave_id)
            else:
                response = client.read_input_registers(address=current_address, count=words_to_read, device_id=device.slave_id)
            if response.isError():
                logger.info(f"Error reading address {current_address} for device {device.name}")
                continue
            logger.info(f"Response: {response.registers}")
            # Map raw values to the address space
            for i, value in enumerate(response.registers):
                base_values[current_address + i] = value
            time.sleep(0.1)
        return base_values

    except Exception as e:
        logger.info(f"Modbus error on device {device.name}: {e}")
        return None
    

"""
Maps raw Modbus data to the defined variables in the VariableAddressMapping model.
Converts values using the defined conversion factors.
"""
def map_variables(base_values, device):
    mapped_values = {}
    mappings = ModbusMappingVariable.objects.filter(device=device)
    logger.info(f"Mapping obtained from database")

    for mapping in mappings:
        try:
            address = int(mapping.address, 16)
            logger.info(f"Mapping: {mapping.var_name}, Start: {mapping.address}")
            logger.info(f"Base values length: {len(base_values)}")

            # Calcolo quanti registri servono per il bit_length richiesto
            num_registers = mapping.bit_length // 16
            registers = []
            for i in range(num_registers):
                reg_addr = address + i  # ogni registro Modbus è 1 word
                if reg_addr in base_values:
                    registers.append(base_values[reg_addr])
                else:
                    raise Exception(f"Missing register at address {hex(reg_addr)} for variable {mapping.var_name}")

            # Combino i registri in un unico valore
            # I registri Modbus sono big-endian per default
            if mapping.endianness == 'big':
                # Use big-endian for both conversion and interpretation
                raw_bytes = b''.join(reg.to_bytes(2, byteorder='big') for reg in registers)
                raw_value = int.from_bytes(raw_bytes, byteorder='big', signed=mapping.is_signed)
            else:
                # Use little-endian for both conversion and interpretation
                raw_bytes = b''.join(reg.to_bytes(2, byteorder='little') for reg in registers)
                raw_value = int.from_bytes(raw_bytes, byteorder='little', signed=mapping.is_signed)
            
            # Applico il conversion factor
            converted_value = convert_value(raw_value, mapping.conversion_factor)
            if converted_value is None:
                raise Exception(f"Invalid conversion factor {mapping.conversion_factor!r} for variable {mapping.var_name}")

            # Salvo il valore nel dizionario
            sanitized_name = sanitize_variable_name(mapping.var_name)
            mapped_values[sanitized_name] = {
                "value": round_to_2_decimals(converted_value),
                "unit": mapping.unit 
            }

        except Exception as e:
            # La variabile non si salva: uno 0 verrebbe preso per una lettura
            # reale (disponibilità, integrale di energia, grafici)
            logger.warning(f"Variable {mapping.var_name} skipped: {e}")
            continue
    json_result = json.dumps(mapped_values, indent=4)
    logger.info(f"Mapped JSON: {json_result}")
    return mapped_values


"""
Computes derived variables using formulas defined in ComputedVariable.
"""
def compute_variables(mapped_values, device):
    computed_vars = ComputedVariable.objects.filter(device=device)
    logger.info(f"Computing values:  {list(computed_vars.values())}")

    results = {}
    for var in computed_vars:
        try:

            "Work the data and adapt it to the sympify formula input data"
            values = {key: value_data["value"] for key, value_data in mapped_values.items()}
            logger.info(f"Worked data: {values}")

            # La formula NON va sanitizzata: trasformerebbe anche operatori e
            # spazi ("Pin - Pout" -> "Pin___Pout"). Nelle formule i nomi delle
            # variabili vanno scritti già con "_" al posto di spazi e "-".
            formula = sympify(a=var.formula)
            logger.info(f"formula: {formula}")

            computed_value = float(formula.evalf(subs=values))
            rounded_value = round_to_2_decimals(computed_value)
            logger.info(f"Computed value: {rounded_value}")

            # Chiave sanitizzata come per le variabili mappate (le view la
            # cercano con sanitize_variable_name)
            results[sanitize_variable_name(var.var_name)] = {
                "value": rounded_value,
                "unit": var.unit
            }
            logger.info(computed_vars)
        except Exception as e:
            # Formula non calcolabile (variabile mancante, divisione per zero...):
            # meglio nessun valore che uno 0 finto
            logger.warning(f"Computed variable {var.var_name} skipped: {e}")
            continue

    # Convert to JSON
    json_result = json.dumps(results, indent=4)
    logger.info(f"Mapped JSON: {json_result}")
    logger.info(f"Computed variables for device {device.name}: {computed_vars}")
    return results

"""
Add an energy increment (kWh) to the cumulative, daily, weekly and monthly
counters of `kind` ('produced' or 'consumed'), starting from the last EnergyData
record. A period counter restarts from 0 when the last record belongs to a
previous period.
"""
def _accumulate_energy(result, kind, increment, last_record, period_starts):
    last_data = last_record.data if last_record else {}
    last_ts = last_record.timestamp if last_record else None

    def previous(key, period_start=None):
        if last_ts is None or (period_start is not None and last_ts < period_start):
            return 0.0
        value = last_data.get(key)
        return value.get('value', 0.0) if isinstance(value, dict) else 0.0

    # 4 decimali: con letture ogni minuto gli incrementi sono dell'ordine di 0.01 kWh
    result[f'Energy_{kind}'] = {'value': round(previous(f'Energy_{kind}') + increment, 4), 'unit': 'kWh'}
    for period, start in zip(('daily', 'weekly', 'monthly'), period_starts):
        key = f'Energy_{period}_{kind}'
        result[key] = {'value': round(previous(key, start) + increment, 4), 'unit': 'kWh'}

"""
Compute energy as power integral
"""
def compute_energy(variables, device_data, energy_data):
    logger.info("Starting to compute integral values")
    try:
        # Get the most recent data
        previous_data = device_data.order_by('-timestamp').first()

        # Define possible power variable names
        power_prod_variable_names = ['Pout', 'Power Production', 'Potenza in uscita']
        power_cons_variable_names = ['Pin', 'Power Consumption', 'Potenza in entrata']
        power_variable_names = ['P', 'Power', 'Potenza']

        power_name = None
        power_cons_variable_name = None
        power_prod_variable_name = None
        is_power_splitted = False

        # Check if the power variable name is configured (MODBUS VERSION)
        is_single_power_variable = False
        for name in power_variable_names:
            if name in variables:
                power_name = name
                is_single_power_variable = True
                break

        logger.info(f"is_single_power_variable: {is_single_power_variable}")
        if is_single_power_variable:
            logger.info(f"power_name: {power_name}")

        # Check if the power variable name is configured (DLMS VERSION - Two variables for power)
        if not is_single_power_variable:
            is_single_power_variable = False
            for name in power_prod_variable_names:
                if name in variables:
                    power_prod_variable_name = name
                    is_power_splitted = True
                    break
            
            logger.info(f"power_prod_variable_name: {power_prod_variable_name}")

            for name in power_cons_variable_names:
                if name in variables:
                    power_cons_variable_name = name
                    is_power_splitted = True
                    break

        # Get timestamp of the dlms reading of the power variable
            timestamp = variables.get('timestamp', None)
            if not timestamp:
                logger.info(f"No timestamp found in variables")
                return None

            logger.info(f"power_cons_variable_name: {power_cons_variable_name}")
            logger.info(f"is_power_splitted: {is_power_splitted}")
        # Contatori precedenti: vivono in EnergyData, non in DeviceData
        previous_energy = energy_data.order_by('-timestamp').first()
        period_starts = local_period_starts()

        # Compute energy for single power variable (MODBUS VERSION)
        if previous_data and is_single_power_variable and not is_power_splitted:
            # Calculate delta time
            delta_time = (datetime.now(timezone.utc) - previous_data.timestamp).total_seconds()

            # Calculate the average value of power
            previous_p = previous_data.data.get(power_name, {}).get('value', 0)
            current_p = variables.get(power_name, {}).get('value', 0)
            average_value = (current_p + previous_p) / 2
            # Energia in kWh: se la potenza è in W la porto in kW
            if str(variables.get(power_name, {}).get('unit') or '').strip() == 'W':
                average_value = average_value / 1000

            # Dopo un'interruzione lunga la media tra due letture lontane non è
            # significativa: non integro, per evitare picchi di energia
            if delta_time > MAX_INTEGRATION_GAP_SECONDS:
                logger.warning(f"Gap of {delta_time:.0f}s since last reading: energy increment skipped")
                energy_increment = 0.0
            else:
                energy_increment = average_value * delta_time / 3600

            # Negative power = energy produced, Positive power = energy consumed
            produced_increment = abs(energy_increment) if energy_increment < 0 else 0.0
            consumed_increment = energy_increment if energy_increment >= 0 else 0.0

            new_energy = {}
            _accumulate_energy(new_energy, 'produced', produced_increment, previous_energy, period_starts)
            _accumulate_energy(new_energy, 'consumed', consumed_increment, previous_energy, period_starts)
            new_energy['Energy'] = {
                'value': round(new_energy['Energy_produced']['value'] + new_energy['Energy_consumed']['value'], 4),
                'unit': 'kWh',
            }

            logger.info(f"Computed energy data: {new_energy}")
            return new_energy

        # Compute energy for split power variables (DLMS VERSION)
        elif is_power_splitted and not is_single_power_variable and (power_prod_variable_name or power_cons_variable_name):
            new_energy = {}

            # Letture DLMS ogni 15 minuti: kWh = kW / 4
            if power_prod_variable_name:
                current_p_produced = variables.get(power_prod_variable_name, {}).get('value', 0)
                _accumulate_energy(new_energy, 'produced', current_p_produced / 4, previous_energy, period_starts)

            if power_cons_variable_name:
                current_p_consumed = variables.get(power_cons_variable_name, {}).get('value', 0)
                _accumulate_energy(new_energy, 'consumed', current_p_consumed / 4, previous_energy, period_starts)

            new_energy['timestamp'] = timestamp
            logger.info(f"Computed energy data: {new_energy}")
            return new_energy

        else:   
            # If none of the above condition applies, the dict returned is empty.
            return None

    except Exception as e:
        logger.error(f"Error during computation: {e}", exc_info=True)
        return None
        
"""
Compute device availability
"""
def compute_device_availability(device, data):
    try:
        now = datetime.now(timezone.utc)
        now_local = convert_to_local_time(now)
        # Mezzanotte locale (aware): un datetime naive verrebbe letto come UTC
        start_of_local_day = local_period_starts(now)[0]
        device_data = DeviceData.objects.filter(device_name=device, timestamp__gte=start_of_local_day)
        if device_data.count() == 0:
            return 0
        else:
            # Check the theoretical number of data at this hour and minute current and convert to second in local time
            hour_of_day = now_local.hour * 3600 + now_local.minute * 60
            interval = settings.CELERY_BEAT_SCHEDULE_INTERVAL if device.protocol == "modbus" else 15 * 60
            num_of_data_expected = math.floor(hour_of_day / interval)
            
            # Prevent division by zero and cap availability at 100%
            if num_of_data_expected == 0:
                availability = 0
            else:
                raw_availability = (device_data.count() / num_of_data_expected) * 100
                # Cap availability at 100% to prevent values above 100%
                availability = round_to_2_decimals(min(raw_availability, 100.0))
            logger.info(f"Actual hour of day: {now_local.hour}:{now_local.minute}")
            logger.info(f"Availability: {availability}")
            logger.info(f"Hour of day: {hour_of_day}")
            logger.info(f"Num of data expected: {num_of_data_expected}")
            logger.info(f"Num of data: {device_data.count()}")
            return availability
    except Exception as e:
        logger.error(f"Error during computation: {e}", exc_info=True)
        return 0

"""
Save device data into the DeviceData model.
"""
def store_data_in_database(device, data):     
    try:
        if is_device_data_already_stored(device, data):
            return
        dev_data = DeviceData.objects.create(
            Gateway=device.Gateway,
            device_name=device,
            data=data
        )
        if hasattr(device, "user"):
            dev_data.user.set(device.user.all())
    except Exception as e:
        logger.info(f"Error while saving the data: {e}")

"""
Store Gateway data into the Database
"""
def store_gateway_data_in_database(gateway, data):
    try:
        # Get current time and convert to local time for consistent quarter-hour calculation
        now = datetime.now(timezone.utc)
        now_local = convert_to_local_time(now)
        
        # Calculate quarter-hour timestamp in local time
        quarter_hour = (now_local.minute // 15) * 15
        target_timestamp = now_local.replace(minute=quarter_hour, second=0, microsecond=0)
        
        # Convert back to UTC for database comparison (since timestamps are stored in UTC)
        target_timestamp_utc = target_timestamp.astimezone(timezone.utc)
        
        # Check if data already exists for this exact hour and minute (quarter-hour)
        existing_data = GatewayData.objects.filter(
            Gateway=gateway,
            timestamp=target_timestamp_utc
        ).first()
        
        if existing_data:
            logger.info(f"Gateway data already exists for {target_timestamp}, skipping save")
            return
        else:
            # Create new gateway data with the quarter-hour timestamp
            gateway_data = GatewayData.objects.create(
                Gateway=gateway,
                timestamp=target_timestamp_utc,
                data=data
            )
            if hasattr(gateway, "user"):
                gateway_data.user.set(gateway.user.all())
            logger.info(f"New gateway data saved for {target_timestamp}")
    except Exception as e:
        logger.info(f"Error while saving the gateway data: {e}")

"""
Store Energy data into the Database
"""
def store_energy_data_in_database(device, data):
    try:
        if is_energy_data_already_stored(device, data):
            return
        energy_data = EnergyData.objects.create(
            Gateway=device.Gateway,
            device_name=device,
            data=data
        )
        if hasattr(device, "user"):
            energy_data.user.set(device.user.all())
    except Exception as e:
        logger.info(f"Error while saving the energy data: {e}")

"""
Check if the device data is already stored in the Database
"""
def is_device_data_already_stored(device, data):
    try:
        if device.protocol == "dlms":
            latest_entry = DeviceData.objects.filter(
                device_name=device
            ).order_by('-timestamp').first()

            if latest_entry:
                existing_data = latest_entry.data
                last_ts = existing_data.get("timestamp")
                current_ts = data.get("timestamp")

                if last_ts and current_ts:
                    last_time = datetime.fromisoformat(last_ts).replace(second=0, microsecond=0)
                    current_time = datetime.fromisoformat(current_ts).replace(second=0, microsecond=0)
                    
                    if last_time == current_time:
                        logger.info(f"Skipped: already stored at {current_time}")
                        return True
        return False
    except Exception as e:
        logger.info(f"Error while checking the device data: {e}")
        return True

"""
Check if the energy data is already stored in the Database
"""
def is_energy_data_already_stored(device, data):
    try:
        if device.protocol == "dlms":
            latest_entry = EnergyData.objects.filter(
                device_name=device
            ).order_by('-timestamp').first()

            if latest_entry:
                existing_data = latest_entry.data
                last_ts = existing_data.get("timestamp")
                current_ts = data.get("timestamp")

                if last_ts and current_ts:
                    last_time = datetime.fromisoformat(last_ts).replace(second=0, microsecond=0)
                    current_time = datetime.fromisoformat(current_ts).replace(second=0, microsecond=0)
                    
                    if last_time == current_time:
                        logger.info(f"Skipped: already stored at {current_time}")
                        return True
        return False
    except Exception as e:
        logger.info(f"Error while checking the energy data: {e}")
        return True

"""
Compute plant availability
"""
def compute_plant_availability(gateway, devices):
    try:
        sum_availability = 0
        enabled_devices_count = 0
        for device in devices:
            if device.is_enabled:
                sum_availability += device.availability
                enabled_devices_count += 1
        
        # Only compute average if there are enabled devices
        if enabled_devices_count == 0:
            return 0
        
        availability = round_to_2_decimals(sum_availability / enabled_devices_count)

        availability = min(availability, 100.0)

        return availability
    except Exception as e:
        logger.info(f"Error while computing the plant availability: {e}")
        return 0

"""
Compute plant performance as (Total daily energy produced / Radiance) * performance factor
"""
def compute_plant_performance(gateway, devices):
    try:
        radiance_value = find_radiance_value(devices)
        power_in = compute_plant_production(gateway, devices)
        
        # Validate inputs
        if not radiance_value or radiance_value <= 0:
            logger.info(f"Invalid radiance value ({radiance_value}) for gateway {gateway.name}")
            return 0
            
        if not power_in or power_in < 0:
            logger.info(f"Invalid power value ({power_in}) for gateway {gateway.name}")
            return 0
            
        # Calculate performance: (power / radiance) * 100 for efficiency percentage
        # Apply performance factor as a multiplier (not multiplied by 100)
        performance =  round_to_2_decimals((power_in / radiance_value)*gateway.performance_factor * 100)
        
        # Cap performance at reasonable values (e.g., 100%)
        performance = min(performance, 100.0)
        
        logger.info(f"Plant performance saved for gateway {gateway.name}: {performance}")
        return performance
    except Exception as e:
        logger.error(f"Error computing plant performance for gateway {gateway.name}: {e}")
        return 0

"""
Helper function to calculate quarter-hour window for power averaging
"""
def get_quarter_hour_window(timestamp):
    """
    Given a timestamp, return the start and end of the previous quarter-hour window.
    For example, if timestamp is 15:17, return 15:00 to 15:15.
    """
    # Round down to the nearest quarter hour
    minute = timestamp.minute
    quarter_hour = (minute // 15) * 15
    
    # Create start of the previous quarter hour (subtract 15 minutes from current quarter hour)
    start_time = timestamp.replace(minute=quarter_hour, second=0, microsecond=0) - timedelta(minutes=15)
    
    # Create end of the previous quarter hour (current quarter hour start)
    end_time = timestamp.replace(minute=quarter_hour, second=0, microsecond=0)
    
    return start_time, end_time

"""
Compute plant production as (Total daily energy produced / Radiance) * performance factor
"""
def compute_plant_production(gateway, devices):
    try:
        # Aggregate the power in of the devices
        power_out = 0
        power_out_variable_names = ['Pout', 'Power Production', 'Potenza in uscita']
        
        for device in devices:
            if device.is_enabled:
                logger.info(f"Computing plant production for device {device.name}")
                latest_device_data = DeviceData.objects.filter(device_name=device).order_by('-timestamp').first()
                logger.info(f"Latest device data: {latest_device_data}")
                
                if latest_device_data and latest_device_data.data:
                    logger.info(f"Latest device data items: {latest_device_data.data.items()}")
                    
                    # Check if device has power variables
                    device_has_power = any(key in latest_device_data.data for key in power_out_variable_names)
                    
                    if device_has_power:
                        # Handle Modbus devices with quarter-hour averaging
                        if device.protocol == "modbus":
                            # Get quarter-hour window for averaging
                            start_time, end_time = get_quarter_hour_window(latest_device_data.timestamp)
                            
                            logger.info(f"PLANT PRODUCTION: Computing quarter-hour window for device {device.name}")
                            logger.info(f"Start time: {start_time}")
                            logger.info(f"End time: {end_time}")

                            # Get all device data within the quarter-hour window
                            quarter_hour_data = DeviceData.objects.filter(
                                device_name=device,
                                timestamp__gte=start_time,
                                timestamp__lt=end_time
                            ).order_by('timestamp')
                            
                            # Calculate average power for each power variable
                            for power_var_name in power_out_variable_names:
                                power_values = []
                                
                                for data_record in quarter_hour_data:
                                    if data_record.data and power_var_name in data_record.data:
                                        value = data_record.data[power_var_name]
                                        
                                        # Safely extract numeric value
                                        if isinstance(value, dict) and 'value' in value:
                                            power_value = value['value']
                                        elif isinstance(value, (int, float)):
                                            power_value = value
                                        else:
                                            continue
                                        
                                        # Validate numeric value
                                        if isinstance(power_value, (int, float)) and not math.isnan(power_value):
                                            power_values.append(power_value)
                                
                                # Calculate average if we have values
                                if power_values:
                                    avg_power = sum(power_values) / len(power_values)
                                    power_out += avg_power
                        
                        # Handle DLMS devices (use latest reading)
                        else:
                            for key, value in latest_device_data.data.items():
                                if key in power_out_variable_names:
                                    # Safely extract numeric value
                                    if isinstance(value, dict) and 'value' in value:
                                        power_value = value['value']
                                    elif isinstance(value, (int, float)):
                                        power_value = value
                                    else:
                                        logger.warning(f"Invalid power value type for device {device.name}: {type(value)}")
                                        continue
                                    
                                    # Validate numeric value
                                    if isinstance(power_value, (int, float)) and not math.isnan(power_value):
                                        power_out += power_value
                                    else:
                                        logger.warning(f"Invalid power value for device {device.name}: {power_value}")
        
        logger.info(f"Plant production saved for gateway {gateway.name}: {power_out}")
        return power_out
    except Exception as e:
        logger.error(f"Error computing plant production for gateway {gateway.name}: {e}")
        return 0


def find_radiance_value(devices):
    try:
        # Define mean radiance keys (higher priority)
        #mean_radiance = ['Mean_Number_Radiance', 'Radiance_Mean', 'Rad_Mean', 'Radiance_Avg', 'Rad_Avg']
        mean_radiance = ['Mean Number Radiance', 'Mean Radiance', 'Radiance Mean', 'Radiance Avg', 'Rad Avg']
        # Define general radiance keys (lower priority)
        radiance = ['Radiance', 'radiance', 'rad', 'Rad']
        
        radiance_value = None
        mean_radiance_value = None
        
        for device in devices:
            if device and device.is_enabled:
                mean_radiance_present = False  # Reset per device
                latest_device_data = DeviceData.objects.filter(device_name=device).order_by('-timestamp').first()

                logger.info(f"RADIANCE")
                logger.info(f"Latest device data: {latest_device_data}")
                if latest_device_data:
                    logger.info(f"Latest device data items: {latest_device_data.data.items()}")
                    logger.info(f"Latest device data timestamp: {latest_device_data.timestamp}")

                if latest_device_data and latest_device_data.data:
                    # Check if device has radiance variables (either mean or general)
                    device_has_mean_radiance = any(key in latest_device_data.data for key in mean_radiance)
                    device_has_radiance = any(key in latest_device_data.data for key in radiance)

                    if device_has_mean_radiance or device_has_radiance:
                        if device.protocol == "modbus":
                            start_time, end_time = get_quarter_hour_window(latest_device_data.timestamp)
                            quarter_hour_data = DeviceData.objects.filter(
                                device_name=device,
                                timestamp__gte=start_time,
                                timestamp__lt=end_time
                            ).order_by('timestamp')
                            logger.info(f"Quarter-hour data: {quarter_hour_data}")

                            # First, check for mean radiance (higher priority)
                            for radiance_value_name in mean_radiance:
                                radiance_values = []
                                for data_record in quarter_hour_data:
                                    if data_record.data and radiance_value_name in data_record.data:
                                        value = data_record.data[radiance_value_name]
                                        if isinstance(value, dict) and 'value' in value:
                                            radiance_values.append(value['value'])
                                        elif isinstance(value, (int, float)):
                                            radiance_values.append(value)
                                        else:
                                            logger.warning(f"Invalid mean radiance value type for device {device.name}: {type(value)}")
                                            continue
                                
                                # If we found valid mean radiance values, calculate average and return immediately
                                if radiance_values:
                                    logger.info(f"Radiance values: {radiance_values}")
                                    avg_radiance = sum(radiance_values) / len(radiance_values)
                                    # Validate the average value
                                    if isinstance(avg_radiance, (int, float)) and not math.isnan(avg_radiance) and avg_radiance >= 0:
                                        logger.info(f"Found mean radiance value {avg_radiance} for device {device.name}")
                                        return avg_radiance
                            
                            # If no mean radiance found, check for general radiance (lower priority)
                            if not mean_radiance_present:
                                logger.info(f"No mean radiance found, checking for general radiance")
                                for radiance_value_name in radiance:
                                    radiance_values = []
                                    for data_record in quarter_hour_data:
                                        if data_record.data and radiance_value_name in data_record.data:
                                            value = data_record.data[radiance_value_name]
                                            if isinstance(value, dict) and 'value' in value:
                                                radiance_values.append(value['value'])
                                            elif isinstance(value, (int, float)):
                                                radiance_values.append(value)
                                            else:
                                                logger.warning(f"Invalid radiance value type for device {device.name}: {type(value)}")
                                                continue
                                    logger.info(f"Radiance values: {radiance_values}")
                                    
                                    # If we found valid radiance values, calculate average and return
                                    if radiance_values:
                                        avg_radiance = sum(radiance_values) / len(radiance_values)
                                        # Validate the average value
                                        if isinstance(avg_radiance, (int, float)) and not math.isnan(avg_radiance) and avg_radiance >= 0:
                                            logger.info(f"Found radiance value {avg_radiance} for device {device.name}")
                                            return avg_radiance

                        else:  # DLMS protocol
                            # First, check for mean radiance (higher priority)
                            for key, value in latest_device_data.data.items():
                                if key in mean_radiance:
                                    # Safely extract numeric value
                                    if isinstance(value, dict) and 'value' in value:
                                        mean_radiance_value = value['value']
                                    elif isinstance(value, (int, float)):
                                        mean_radiance_value = value
                                    else:
                                        logger.warning(f"Invalid mean radiance value type for device {device.name}: {type(value)}")
                                        continue
                                    
                                    # Validate numeric value
                                    if isinstance(mean_radiance_value, (int, float)) and not math.isnan(mean_radiance_value) and mean_radiance_value >= 0:
                                        mean_radiance_present = True
                                        logger.info(f"Found mean radiance value {mean_radiance_value} for device {device.name}")
                                        return mean_radiance_value
                                    else:
                                        continue
                            
                            # If no mean radiance found, check for general radiance (lower priority)
                            if not mean_radiance_present:
                                for key, value in latest_device_data.data.items():
                                    if key in radiance:
                                        # Safely extract numeric value
                                        if isinstance(value, dict) and 'value' in value:
                                            radiance_value = value['value']
                                        elif isinstance(value, (int, float)):
                                            radiance_value = value
                                        else:
                                            logger.warning(f"Invalid radiance value type for device {device.name}: {type(value)}")
                                            continue
                                        
                                        # Validate numeric value
                                        if isinstance(radiance_value, (int, float)) and not math.isnan(radiance_value) and radiance_value >= 0:
                                            logger.info(f"Found radiance value {radiance_value} for device {device.name}")
                                            return radiance_value
                                        else:
                                            continue
                
                logger.info(f"No radiance value found for device {device.name}")
        
        # Return None if no radiance value found in any device
        logger.info("No radiance value found in any device")
        return None
            
    except Exception as e:
        logger.error(f"Error finding radiance value: {e}")
        return None