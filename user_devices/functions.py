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
from .helper_funcs import sanitize_variable_name, convert_value, convert_to_local_time, round_to_2_decimals
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_WORDS_PER_READ = 12
TIMEOUT = 5                 # Timeout per la connessione

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
            response = requests.post(rest_api_call, params=params, json=payload)
            logger.info(f"Response: {response.json()}")

            if response.ok:
                data = response.json()
                logger.info(f"Data: {data}")

                # Aggregate the values and the timestamps for each column_idx
                reading = []
                mapped_values = {}
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
Reads every ModbusReadBlock of the device, splitting each block in chunks
of MAX_WORDS_PER_READ. Returns {(register_type, address): raw_value}.
"""
def read_modbus_registers(device, client):
    try:
        base_values = {}
        for block in device.read_blocks.all():
            start_address = int(block.start_address, 16)
            word_count = block.word_count
            logger.info(f"Block {block.register_type} start: {start_address}, word count: {word_count}")

            # Split reads into chunks of MAX_WORDS_PER_READ
            for offset in range(0, word_count, MAX_WORDS_PER_READ):
                current_address = start_address + offset
                logger.info(f"Start Address: {current_address}")
                words_to_read = min(MAX_WORDS_PER_READ, word_count - offset)
                if block.register_type == 'holding':
                    response = client.read_holding_registers(address=current_address, count=words_to_read, device_id=device.slave_id)
                else:
                    response = client.read_input_registers(address=current_address, count=words_to_read, device_id=device.slave_id)
                if response.isError():
                    logger.info(f"Error reading address {current_address} for device {device.name}")
                    continue
                logger.info(f"Response: {response.registers}")
                # Map raw values to the address space
                for i, value in enumerate(response.registers):
                    base_values[(block.register_type, current_address + i)] = value
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
                if (mapping.register_type, reg_addr) in base_values:
                    registers.append(base_values[(mapping.register_type, reg_addr)])
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
            converted_value = convert_value(raw_value - (mapping.offset or 0), mapping.conversion_factor)

            # Salvo il valore nel dizionario
            sanitized_name = sanitize_variable_name(mapping.var_name)
            mapped_values[sanitized_name] = {
                "value": round_to_2_decimals(converted_value),
                "unit": mapping.unit 
            }

        except Exception as e:
            sanitized_name = sanitize_variable_name(mapping.var_name)
            mapped_values[sanitized_name] = {
                "value": 0,
                "unit": mapping.unit if hasattr(mapping, "unit") else "N/A"
            }
            logger.info(f"Error while mapping the values: {e}")
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

            sanitized_formula = sanitize_variable_name(var.formula)
            formula = sympify(a=sanitized_formula)
            logger.info(f"formula: {formula}")

            computed_value = float(formula.evalf(subs=values))
            rounded_value = round_to_2_decimals(computed_value)
            logger.info(f"Computed value: {rounded_value}")

            results[var.var_name] = {
                "value": rounded_value,
                "unit": var.unit 
            }
            logger.info(computed_vars)
        except Exception as e:
            results[var.var_name] = {
                "value": 0,
                "unit": var.unit if hasattr(var, "unit") else "N/A"
            }
            logger.info(f"Error while mapping the values: {e}")
            continue

    # Convert to JSON
    json_result = json.dumps(results, indent=4)
    logger.info(f"Mapped JSON: {json_result}")
    logger.info(f"Computed variables for device {device.name}: {computed_vars}")
    return results

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
        # Compute energy for single power variable (DLMS VERSION)
        if previous_data and is_single_power_variable and not is_power_splitted:
            # Calculate delta time
            delta_time = (datetime.now(timezone.utc) - previous_data.timestamp).total_seconds()

            # Calculate the average value of power
            previous_p = previous_data.data.get(power_name, {}).get('value', 0)
            current_p = variables.get(power_name, {}).get('value', 0)
            average_value = (current_p + previous_p) / 2

            # Compute the energy increment for this period
            energy_increment = round_to_2_decimals(average_value * delta_time)

            # Get previous energy values
            previous_energy = previous_data.data.get('Energy', {}).get('value', 0.0)
            previous_energy_produced = previous_data.data.get('Energy_produced', {}).get('value', 0.0)
            previous_energy_consumed = previous_data.data.get('Energy_consumed', {}).get('value', 0.0)

            # Update produced/consumed based on the sign of energy increment
            # Negative power = energy produced, Positive power = energy consumed
            if average_value >= 0:
                # Consumption (positive power)
                energy_consumed = round_to_2_decimals(previous_energy_consumed + energy_increment)
                energy_produced = round_to_2_decimals(previous_energy_produced)
            else:
                # Production (negative power)
                energy_produced = round_to_2_decimals(previous_energy_produced + abs(energy_increment))
                energy_consumed = round_to_2_decimals(previous_energy_consumed)

            # Total energy is still the running sum of all increments
            integral_value = round_to_2_decimals(previous_energy + energy_increment)

            logger.info(f"Computed integral value: {integral_value}")
            
            # Compute energy for different periods
            now = datetime.now(timezone.utc)

            # Define date ranges
            start_of_day_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_of_day_local = convert_to_local_time(start_of_day_utc)
            start_of_day_filter = start_of_day_local.astimezone(timezone.utc)
            
            # Daily energy
            daily_records = device_data.filter(timestamp__gte=start_of_day_filter)
            daily_produced = daily_records.aggregate(
                total=Sum(RawSQL("CASE WHEN CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION) < 0 THEN ABS(CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION)) ELSE 0 END", []))
            )['total'] or 0.0
            daily_consumed = daily_records.aggregate(
                total=Sum(RawSQL("CASE WHEN CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION) >= 0 THEN CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION) ELSE 0 END", []))
            )['total'] or 0.0

            # Weekly energy
            start_of_week_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).isocalendar().weekday(1)
            start_of_week_local = convert_to_local_time(start_of_week_utc)
            start_of_week_filter = start_of_week_local.astimezone(timezone.utc)
            weekly_records = device_data.filter(timestamp__gte=start_of_week_filter)
            weekly_produced = weekly_records.aggregate(
                total=Sum(RawSQL("CASE WHEN CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION) < 0 THEN ABS(CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION)) ELSE 0 END", []))
            )['total'] or 0.0
            weekly_consumed = weekly_records.aggregate(
                total=Sum(RawSQL("CASE WHEN CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION) >= 0 THEN CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION) ELSE 0 END", []))
            )['total'] or 0.0

            # Monthly energy
            start_of_month_utc = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            start_of_month_local = convert_to_local_time(start_of_month_utc)
            start_of_month_filter = start_of_month_local.astimezone(timezone.utc)
            monthly_records = device_data.filter(timestamp__gte=start_of_month_filter)
            monthly_produced = monthly_records.aggregate(
                total=Sum(RawSQL("CASE WHEN CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION) < 0 THEN ABS(CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION)) ELSE 0 END", []))
            )['total'] or 0.0
            monthly_consumed = monthly_records.aggregate(
                total=Sum(RawSQL("CASE WHEN CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION) >= 0 THEN CAST(data->'"+power_name+"'->>'value' AS DOUBLE PRECISION) ELSE 0 END", []))
            )['total'] or 0.0

            # Apply time factor to get energy values (power × time)
            time_factor = delta_time  # This is approximate - ideally would sum actual time intervals

            # Store all computed values in a structured dictionary
            energy_data = {
                'Energy': {'value': round_to_2_decimals(energy_produced + energy_consumed), 'unit': 'kWh'},
                'Energy_produced': {'value': energy_produced, 'unit': 'kWh'},
                'Energy_consumed': {'value': energy_consumed, 'unit': 'kWh'},
                
                'Energy_daily_produced': {'value': round_to_2_decimals(daily_produced * time_factor), 'unit': 'kWh'},
                'Energy_daily_consumed': {'value': round_to_2_decimals(daily_consumed * time_factor), 'unit': 'kWh'},
                
                'Energy_weekly_produced': {'value': round_to_2_decimals(weekly_produced * time_factor), 'unit': 'kWh'},
                'Energy_weekly_consumed': {'value': round_to_2_decimals(weekly_consumed * time_factor), 'unit': 'kWh'},
                
                'Energy_monthly_produced': {'value': round_to_2_decimals(monthly_produced * time_factor), 'unit': 'kWh'},
                'Energy_monthly_consumed': {'value': round_to_2_decimals(monthly_consumed * time_factor), 'unit': 'kWh'},
            }


            logger.info(f"Computed energy data: {energy_data}")
            return energy_data
        
        # Compute energy for split power variables (DLMS VERSION)
        elif is_power_splitted and not is_single_power_variable and (power_prod_variable_name or power_cons_variable_name):
            
            logger.info(f"Computing the time intervals for the energy data")

            # Daily, weekly, monthly energy produced and consumed
            now = datetime.now(timezone.utc)

            # Start of UTC day
            start_of_day_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
            # Convert to local (Django TZ)
            start_of_day_local = convert_to_local_time(start_of_day_utc)

            # For filtering DB (which expects UTC), convert back. Timestamps are utc in django
            start_of_day_filter = start_of_day_local.astimezone(timezone.utc)

            daily_records = energy_data.filter(timestamp__gte=start_of_day_filter)

            # Start of UTC week
            start_of_week_utc = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())

            # Convert to local (Django TZ)
            start_of_week_local = convert_to_local_time(start_of_week_utc)

            # For filtering DB (which expects UTC), convert back. Timestampas are utc in django
            start_of_week_filter = start_of_week_local.astimezone(timezone.utc)

            weekly_records = energy_data.filter(timestamp__gte=start_of_week_filter)

            # Start of UTC month
            start_of_month_utc = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            # Convert to local (Django TZ)
            start_of_month_local = convert_to_local_time(start_of_month_utc)

            # For filtering DB (which expects UTC), convert back. Timestampas are utc in django
            start_of_month_filter = start_of_month_local.astimezone(timezone.utc)
            monthly_records = energy_data.filter(timestamp__gte=start_of_month_filter)

            # Create a structured dictionary for the energy data
            energy_data = {}

            # Daily, weekly, monthly energy produced
            if power_prod_variable_name:

                logger.info(f"Computing energy for power produced: {power_prod_variable_name}")

                # Get the current value of power produced
                current_p_produced = variables.get(power_prod_variable_name, {}).get('value', 0)

                # Compute the energy increment for this period
                energy_produced_increment = round_to_2_decimals(current_p_produced / 4)

                # Get the previous energy produced
                previous_energy_produced = previous_data.data.get('Energy_produced', {}).get('value', 0.0) if previous_data else 0.0

                # Update the energy produced
                energy_produced = round_to_2_decimals(previous_energy_produced + energy_produced_increment)

                # Daily, weekly, monthly energy produced variables
                last_daily_record = daily_records.order_by('-timestamp').first()
                if last_daily_record and 'Energy_daily_produced' in last_daily_record.data:
                    daily_produced = last_daily_record.data['Energy_daily_produced'].get('value', 0.0)
                else:
                    daily_produced = 0.0
                daily_produced = round_to_2_decimals(daily_produced + energy_produced_increment)

                last_weekly_record = weekly_records.order_by('-timestamp').first()
                if last_weekly_record and 'Energy_weekly_produced' in last_weekly_record.data:
                    weekly_produced = last_weekly_record.data['Energy_weekly_produced'].get('value', 0.0)
                else:
                    weekly_produced = 0.0
                weekly_produced = round_to_2_decimals(weekly_produced + energy_produced_increment)

                last_monthly_record = monthly_records.order_by('-timestamp').first()
                if last_monthly_record and 'Energy_monthly_produced' in last_monthly_record.data:
                    monthly_produced = last_monthly_record.data['Energy_monthly_produced'].get('value', 0.0)
                else:
                    monthly_produced = 0.0
                monthly_produced = round_to_2_decimals(monthly_produced + energy_produced_increment)

                # Add energy produced to the energy data dictionary
                energy_data['Energy_produced'] = {'value': energy_produced, 'unit': 'kWh'}
                energy_data['Energy_daily_produced'] = {'value': daily_produced, 'unit': 'kWh'}
                energy_data['Energy_weekly_produced'] = {'value': weekly_produced, 'unit': 'kWh'}
                energy_data['Energy_monthly_produced'] = {'value': monthly_produced, 'unit': 'kWh'}

            # Daily, weekly, monthly energy consumed
            if power_cons_variable_name:

                logger.info(f"Computing energy for power consumed: {power_cons_variable_name}")

                # Get the current value of power consumed
                current_p_consumed = variables.get(power_cons_variable_name, {}).get('value', 0)

                # Compute the energy increment for this period
                energy_consumed_increment = round_to_2_decimals(current_p_consumed / 4)

                # Get the previous energy consumed
                previous_energy_consumed = previous_data.data.get('Energy_consumed', {}).get('value', 0.0) if previous_data else 0.0

                # Update the energy consumed
                energy_consumed = round_to_2_decimals(previous_energy_consumed + energy_consumed_increment)

                # Daily, weekly, monthly energy consumed variables
                last_daily_record_cons = daily_records.order_by('-timestamp').first()
                if last_daily_record_cons and 'Energy_daily_consumed' in last_daily_record_cons.data:
                    daily_consumed = last_daily_record_cons.data['Energy_daily_consumed'].get('value', 0.0)
                else:
                    daily_consumed = 0.0
                daily_consumed = round_to_2_decimals(daily_consumed + energy_consumed_increment)

                last_weekly_record_cons = weekly_records.order_by('-timestamp').first()
                if last_weekly_record_cons and 'Energy_weekly_consumed' in last_weekly_record_cons.data:
                    weekly_consumed = last_weekly_record_cons.data['Energy_weekly_consumed'].get('value', 0.0)
                else:
                    weekly_consumed = 0.0
                weekly_consumed = round_to_2_decimals(weekly_consumed + energy_consumed_increment)

                last_monthly_record_cons = monthly_records.order_by('-timestamp').first()
                if last_monthly_record_cons and 'Energy_monthly_consumed' in last_monthly_record_cons.data:
                    monthly_consumed = last_monthly_record_cons.data['Energy_monthly_consumed'].get('value', 0.0)
                else:
                    monthly_consumed = 0.0
                monthly_consumed = round_to_2_decimals(monthly_consumed + energy_consumed_increment)

                # Add energy consumed to the energy data dictionary
                energy_data['Energy_consumed'] = {'value': energy_consumed, 'unit': 'kWh'}
                energy_data['Energy_daily_consumed'] = {'value': daily_consumed, 'unit': 'kWh'}
                energy_data['Energy_weekly_consumed'] = {'value': weekly_consumed, 'unit': 'kWh'}
                energy_data['Energy_monthly_consumed'] = {'value': monthly_consumed, 'unit': 'kWh'}

            energy_data['timestamp'] = timestamp
            logger.info(f"Computed energy data: {energy_data}")
            return energy_data

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
        # Get start of local day in UTC
        start_of_local_day = datetime(now_local.year, now_local.month, now_local.day, 0, 0, 0)
        start_of_local_day_utc = start_of_local_day.astimezone(timezone.utc)
        device_data = DeviceData.objects.filter(device_name=device, timestamp__gte=start_of_local_day_utc)
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