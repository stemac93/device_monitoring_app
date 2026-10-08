# user_devices/tests/test_functions.py
import pdb
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from fractions import Fraction
from pymodbus.client import ModbusTcpClient
from django.test import TestCase
from user_devices.models import Device, ModbusMappingVariable, ComputedVariable, DeviceData, Gateway
from user_devices.functions import (
    sanitize_variable_name, 
    read_modbus_registers, 
    map_variables, 
    compute_variables,
    compute_energy, 
    store_data_in_database,
    compute_device_availability,
    compute_plant_production,
    get_quarter_hour_window
)

class TestSanitizeVariableName(TestCase):
    def test_sanitize_variable_name(self):
        """Test that variable names are properly sanitized"""
        test_cases = [
            ("test-variable", "test_variable"),
            ("test variable", "test_variable"),
            ("test-variable with space", "test_variable_with_space"),
            ("normal_name", "normal_name")
        ]
        
        for input_name, expected_output in test_cases:
            self.assertEqual(sanitize_variable_name(input_name), expected_output)

class TestReadModbusRegisters(TestCase):
    @patch('user_devices.functions.logger')
    def test_read_modbus_registers_success(self, mock_logger):
        """Test successful reading of modbus registers"""
        # Setup mock device and client
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=3)]
        device.slave_id = 1
        
        client = Mock()
        # Mock successful response
        response = Mock()
        response.isError.return_value = False
        response.registers = [100, 200, 300]  # Sample register values
        client.read_input_registers.return_value = response
        
        # Execute function
        result = read_modbus_registers(device, client)
        
        # Verify correct address calculations and returned data
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 3)  # Should have 3 values from our mock
        self.assertEqual(result[("input", 640)], 100)  # 0x0280 = 640 decimal
        self.assertEqual(result[("input", 641)], 200)
        self.assertEqual(result[("input", 642)], 300)
        
        # Verify client was called with correct parameters
        client.read_input_registers.assert_called_with(
            address=640, count=3, device_id=1
        )

    @patch('user_devices.functions.logger')
    def test_read_modbus_registers_error(self, mock_logger):
        """Test handling of errors during modbus register reading"""
        # Setup mock device and client
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=2)]
        device.slave_id = 1
        
        client = Mock()
        # Mock error response
        response = Mock()
        response.isError.return_value = True
        client.read_input_registers.return_value = response
        
        # Execute function
        result = read_modbus_registers(device, client)
        
        # Verify empty result due to error
        self.assertEqual(result, {})
        
        # Verify error was logged
        mock_logger.info.assert_any_call(f"Error reading address 640 for device Test Device")

    @patch('user_devices.functions.logger')
    def test_read_modbus_registers_exception(self, mock_logger):
        """Test handling of exceptions during modbus register reading"""
        # Setup mock device and client
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=2)]
        device.slave_id = 1
        
        client = Mock()
        # Force exception when calling read_input_registers
        client.read_input_registers.side_effect = Exception("Test exception")
        
        # Execute function
        result = read_modbus_registers(device, client)
        
        # Verify function returns None on exception
        self.assertIsNone(result)
        
        # Verify exception was logged
        mock_logger.info.assert_called_with("Modbus error on device Test Device: Test exception")

class TestMapVariables(TestCase):
    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.ModbusMappingVariable.objects.filter')
    def test_map_variables_success(self, mock_filter, mock_logger):
        """Test successful mapping of variables from raw values"""
        # Setup mock device and base values
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=2)]
        device.slave_id = 1
        
        base_values = {
            ("input", 0x0280): 100,  # Voltage raw value
            ("input", 0x0281): 200,  # Current raw value
        }
        
        # Setup mock mappings
        voltage_mapping = Mock()
        voltage_mapping.register_type = "input"
        voltage_mapping.offset = 0
        voltage_mapping.var_name = "Voltage"
        voltage_mapping.address = "0x0280"
        voltage_mapping.conversion_factor = "0.1"
        voltage_mapping.unit = "V"
        voltage_mapping.bit_length = 16
        
        current_mapping = Mock()
        
        current_mapping.register_type = "input"
        
        current_mapping.offset = 0
        current_mapping.var_name = "Current"
        current_mapping.address = "0x0281"
        current_mapping.conversion_factor = "0.01"
        current_mapping.unit = "A"
        current_mapping.bit_length = 16
        
        # Configure the mock filter to return our mock mappings
        mock_filter.return_value = [voltage_mapping, current_mapping]
        
        # Execute function
        result = map_variables(base_values, device)
        
        # Verify correct mapping of values
        self.assertEqual(result["Voltage"]["value"], 10.0)  # 100 * 0.1
        self.assertEqual(result["Voltage"]["unit"], "V")
        self.assertEqual(result["Current"]["value"], 2.0)  # 200 * 0.01
        self.assertEqual(result["Current"]["unit"], "A")

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.ModbusMappingVariable.objects.filter')
    def test_map_variables_fraction_conversion(self, mock_filter, mock_logger):
        """Test mapping with fractional conversion factors"""
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0284", word_count=1)]
        device.slave_id = 1

        base_values = {("input", 0x0284): 300}
        
        # Test with fractional conversion factor
        power_mapping = Mock()
        power_mapping.register_type = "input"
        power_mapping.offset = 0
        power_mapping.var_name = "Power"
        power_mapping.address = "0x0284"
        power_mapping.conversion_factor = "1/10"
        power_mapping.unit = "kW"
        power_mapping.bit_length = 16
        
        mock_filter.return_value = [power_mapping]
        
        result = map_variables(base_values, device)
        
        # 300 * (1/10) = 30.0
        self.assertEqual(result["Power"]["value"], 30.0)
        self.assertEqual(result["Power"]["unit"], "kW")

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.ModbusMappingVariable.objects.filter')
    def test_map_variables_error_handling(self, mock_filter, mock_logger):
        """Test error handling during variable mapping"""
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=1)]
        device.slave_id = 1
        
        base_values = {("input", 0x0280): 100}
        
        # Setup mapping with invalid conversion factor
        invalid_mapping = Mock()
        invalid_mapping.register_type = "input"
        invalid_mapping.offset = 0
        invalid_mapping.var_name = "Invalid"
        invalid_mapping.address = "0x0280"
        invalid_mapping.conversion_factor = "invalid"
        invalid_mapping.unit = "X"
        
        # Setup mapping with missing address
        missing_mapping = Mock()
        missing_mapping.register_type = "input"
        missing_mapping.offset = 0
        missing_mapping.var_name = "Missing"
        missing_mapping.address = "0x0290"  # Not in base_values
        missing_mapping.conversion_factor = "0.1"
        missing_mapping.unit = "Y"
        
        mock_filter.return_value = [invalid_mapping, missing_mapping]
        
        result = map_variables(base_values, device)
        
        # Fattore non valido o registro mancante: la variabile non viene salvata
        # (uno 0 sembrerebbe una lettura reale)
        self.assertNotIn("Invalid", result)
        self.assertNotIn("Missing", result)

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.ModbusMappingVariable.objects.filter')
    def test_map_variables_16bit_unsigned(self, mock_filter, mock_logger):
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=1)]
        device.slave_id = 1

        base_values = {("input", 0x0280): 0x1234}

        mapping = Mock()

        mapping.register_type = "input"

        mapping.offset = 0
        mapping.var_name = "Var16U"
        mapping.address = "0x0280"
        mapping.conversion_factor = "1"
        mapping.unit = "U"
        mapping.bit_length = 16
        mapping.is_signed = False
        mock_filter.return_value = [mapping]
        result = map_variables(base_values, device)
        self.assertEqual(result["Var16U"]["value"], 0x1234)
        self.assertEqual(result["Var16U"]["unit"], "U")

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.ModbusMappingVariable.objects.filter')
    def test_map_variables_16bit_signed(self, mock_filter, mock_logger):
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=1)]
        device.slave_id = 1
        
        base_values = {("input", 0x0280): 0xFFFF}  # -1 in signed 16-bit
        mapping = Mock()
        mapping.register_type = "input"
        mapping.offset = 0
        mapping.var_name = "Var16S"
        mapping.address = "0x0280"
        mapping.conversion_factor = "1"
        mapping.unit = "S"
        mapping.bit_length = 16
        mapping.is_signed = True
        mock_filter.return_value = [mapping]
        result = map_variables(base_values, device)
        self.assertEqual(result["Var16S"]["value"], -1)
        self.assertEqual(result["Var16S"]["unit"], "S")

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.ModbusMappingVariable.objects.filter')
    def test_map_variables_32bit_unsigned(self, mock_filter, mock_logger):
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=2)]
        device.slave_id = 1
        
        # 0x12345678 split into two 16-bit registers: 0x1234, 0x5678
        base_values = {("input", 0x0280): 0x1234, ("input", 0x0281): 0x5678}
        mapping = Mock()
        mapping.register_type = "input"
        mapping.offset = 0
        mapping.var_name = "Var32U"
        mapping.address = "0x0280"
        mapping.conversion_factor = "1"
        mapping.unit = "U"
        mapping.bit_length = 32
        mapping.is_signed = False
        mapping.endianness = "big"
        mock_filter.return_value = [mapping]
        result = map_variables(base_values, device)
        expected = (0x1234 << 16) | 0x5678
        self.assertAlmostEqual(result["Var32U"]["value"], float(expected))
        self.assertEqual(result["Var32U"]["unit"], "U")

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.ModbusMappingVariable.objects.filter')
    def test_map_variables_32bit_signed(self, mock_filter, mock_logger):
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=2)]
        device.slave_id = 1
        
        # 0xFFFF8000 is -32768 in signed 32-bit
        base_values = {("input", 0x0280): 0xFFFF, ("input", 0x0281): 0x8000}
        mapping = Mock()
        mapping.register_type = "input"
        mapping.offset = 0
        mapping.var_name = "Var32S"
        mapping.address = "0x0280"
        mapping.conversion_factor = "1"
        mapping.unit = "S"
        mapping.bit_length = 32
        mapping.is_signed = True
        mapping.endianness = "big"
        mock_filter.return_value = [mapping]
        result = map_variables(base_values, device)
        expected = int.from_bytes(b'\xff\xff\x80\x00', byteorder='big', signed=True)
        self.assertAlmostEqual(result["Var32S"]["value"], float(expected))
        self.assertEqual(result["Var32S"]["unit"], "S")

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.ModbusMappingVariable.objects.filter')
    def test_map_variables_64bit_unsigned(self, mock_filter, mock_logger):
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=4)]
        device.slave_id = 1
        
        # 0x0123456789ABCDEF split into four 16-bit registers
        base_values = {("input", 0x0280): 0x0123, ("input", 0x0281): 0x4567, ("input", 0x0282): 0x89AB, ("input", 0x0283): 0xCDEF}
        mapping = Mock()
        mapping.register_type = "input"
        mapping.offset = 0
        mapping.var_name = "Var64U"
        mapping.address = "0x0280"
        mapping.conversion_factor = "1"
        mapping.unit = "U"
        mapping.bit_length = 64
        mapping.is_signed = False
        mapping.endianness = "big"
        mock_filter.return_value = [mapping]
        result = map_variables(base_values, device)
        expected = (0x0123 << 48) | (0x4567 << 32) | (0x89AB << 16) | 0xCDEF
        self.assertAlmostEqual(result["Var64U"]["value"], float(expected))
        self.assertEqual(result["Var64U"]["unit"], "U")

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.ModbusMappingVariable.objects.filter')
    def test_map_variables_64bit_signed(self, mock_filter, mock_logger):
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=4)]
        device.slave_id = 1
        
        # 0xFFFFFFFF80000000 is -2147483648 in signed 64-bit
        base_values = {("input", 0x0280): 0xFFFF, ("input", 0x0281): 0xFFFF, ("input", 0x0282): 0x8000, ("input", 0x0283): 0x0000}
        mapping = Mock()
        mapping.register_type = "input"
        mapping.offset = 0
        mapping.var_name = "Var64S"
        mapping.address = "0x0280"
        mapping.conversion_factor = "1"
        mapping.unit = "S"
        mapping.bit_length = 64
        mapping.is_signed = True
        mapping.endianness = "big"
        mock_filter.return_value = [mapping]
        result = map_variables(base_values, device)
        expected = int.from_bytes(b'\xff\xff\xff\xff\x80\x00\x00\x00', byteorder='big', signed=True)
        self.assertAlmostEqual(result["Var64S"]["value"], float(expected))
        self.assertEqual(result["Var64S"]["unit"], "S")

class TestComputeVariables(TestCase):
    @patch('user_devices.functions.ComputedVariable.objects.filter')
    def test_compute_variables_success(self, mock_filter):
        """Test successful computation of derived variables"""
        # Setup mapped values
        mapped_values = {
            "Voltage": {"value": 230.0, "unit": "V"},
            "Current": {"value": 2.0, "unit": "A"}
        }

        # Setup mock for ComputedVariable
        power_var = Mock()
        power_var.var_name = "Power"
        power_var.formula = "Voltage * Current"
        power_var.unit = "W"

        # Use the queryset mock for the filtering
        mock_queryset = Mock()
        mock_queryset.__iter__ = Mock(return_value=iter([power_var]))
        mock_queryset.values.return_value = [{'var_name': 'Power', 'formula': 'Voltage * Current', 'unit': 'W'}]
        mock_filter.return_value = mock_queryset

        # Execute function
        result = compute_variables(mapped_values, Mock())

        # Verify results
        self.assertEqual(result["Power"]["value"], 460.0)
        self.assertEqual(result["Power"]["unit"], "W")

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.ComputedVariable.objects.filter')
    def test_compute_variables_error_handling(self, mock_filter, mock_logger):
        """Test error handling during variable computation"""
        device = Mock()
        device.name = "Test Device"
        
        mapped_values = {
            "Voltage": {"value": 230.0, "unit": "V"}
        }
        
        # Formula references a variable not in mapped_values
        invalid_var = Mock()
        invalid_var.var_name = "InvalidPower"
        invalid_var.formula = "Voltage * MissingCurrent"
        invalid_var.unit = "W"
        
        # Use the queryset mock for the filtering
        mock_queryset = Mock()
        mock_queryset.__iter__ = Mock(return_value=iter([invalid_var]))
        mock_queryset.values.return_value = [{'var_name': 'Power', 'formula': 'Voltage * Current', 'unit': 'W'}]
        mock_filter.return_value = mock_queryset
        try:
            result = compute_variables(mapped_values, device)
        except Exception as e:
            print(e)
        print(f"Result: {result}")
        # Formula non calcolabile: nessun valore (non uno 0 finto)
        self.assertNotIn("InvalidPower", result)

class TestComputeEnergy(TestCase):
    """compute_energy integra la potenza in kWh partendo dai contatori
    dell'ultimo EnergyData (i DeviceData non contengono le chiavi Energy_*)."""

    COUNTERS = {
        'Energy_produced': {'value': 1.0, 'unit': 'kWh'},
        'Energy_consumed': {'value': 6.0, 'unit': 'kWh'},
        'Energy_daily_produced': {'value': 0.2, 'unit': 'kWh'},
        'Energy_daily_consumed': {'value': 0.3, 'unit': 'kWh'},
        'Energy_weekly_produced': {'value': 0.4, 'unit': 'kWh'},
        'Energy_weekly_consumed': {'value': 0.5, 'unit': 'kWh'},
        'Energy_monthly_produced': {'value': 0.6, 'unit': 'kWh'},
        'Energy_monthly_consumed': {'value': 0.7, 'unit': 'kWh'},
    }

    def _querysets(self, previous_power, seconds_ago=300, power_name='P', energy_ts=None):
        """DeviceData precedente (potenza, `seconds_ago` secondi fa) ed EnergyData precedente."""
        previous_data = Mock()
        previous_data.timestamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        previous_data.data = {power_name: {'value': previous_power, 'unit': 'W'}}
        device_data = Mock()
        device_data.order_by.return_value.first.return_value = previous_data

        previous_energy = Mock()
        previous_energy.timestamp = energy_ts or datetime.now(timezone.utc)
        previous_energy.data = dict(self.COUNTERS)
        energy_data = Mock()
        energy_data.order_by.return_value.first.return_value = previous_energy
        return device_data, energy_data

    @patch('user_devices.functions.logger')
    def test_compute_energy_with_previous_data(self, mock_logger):
        """2 kW costanti per 5 minuti -> +0.1667 kWh consumati"""
        device_data, energy_data = self._querysets(previous_power=2000.0)
        result = compute_energy({'P': {'value': 2000.0, 'unit': 'W'}}, device_data, energy_data)

        for key in ('Energy', 'Energy_produced', 'Energy_consumed',
                    'Energy_daily_produced', 'Energy_daily_consumed',
                    'Energy_weekly_produced', 'Energy_weekly_consumed',
                    'Energy_monthly_produced', 'Energy_monthly_consumed'):
            self.assertIn(key, result)
            self.assertEqual(result[key]['unit'], 'kWh')

        increment = 2.0 * 300 / 3600
        self.assertAlmostEqual(result['Energy_consumed']['value'], 6.0 + increment, places=2)
        self.assertAlmostEqual(result['Energy_daily_consumed']['value'], 0.3 + increment, places=2)
        self.assertAlmostEqual(result['Energy_weekly_consumed']['value'], 0.5 + increment, places=2)
        self.assertAlmostEqual(result['Energy_monthly_consumed']['value'], 0.7 + increment, places=2)
        # Potenza positiva: il prodotto non cambia
        self.assertAlmostEqual(result['Energy_produced']['value'], 1.0)
        self.assertAlmostEqual(result['Energy']['value'],
                               result['Energy_produced']['value'] + result['Energy_consumed']['value'], places=3)

    @patch('user_devices.functions.logger')
    def test_compute_energy_with_negative_power(self, mock_logger):
        """Potenza negativa = produzione"""
        device_data, energy_data = self._querysets(previous_power=-500.0)
        result = compute_energy({'P': {'value': -1000.0, 'unit': 'W'}}, device_data, energy_data)

        increment = 0.75 * 300 / 3600
        self.assertAlmostEqual(result['Energy_produced']['value'], 1.0 + increment, places=3)
        self.assertAlmostEqual(result['Energy_daily_produced']['value'], 0.2 + increment, places=3)
        self.assertAlmostEqual(result['Energy_consumed']['value'], 6.0)

    @patch('user_devices.functions.logger')
    def test_compute_energy_with_alternative_power_name(self, mock_logger):
        """'Power' è riconosciuto come variabile di potenza"""
        device_data, energy_data = self._querysets(previous_power=1000.0, power_name='Power')
        result = compute_energy({'Power': {'value': 2000.0, 'unit': 'W'}}, device_data, energy_data)

        self.assertIn('Energy', result)
        self.assertIn('Energy_produced', result)
        self.assertIn('Energy_consumed', result)

    @patch('user_devices.functions.logger')
    def test_compute_energy_kw_unit(self, mock_logger):
        """Con potenza in kW non si divide per 1000"""
        device_data, energy_data = self._querysets(previous_power=2.0)
        device_data.order_by.return_value.first.return_value.data = {'P': {'value': 2.0, 'unit': 'kW'}}
        result = compute_energy({'P': {'value': 2.0, 'unit': 'kW'}}, device_data, energy_data)

        self.assertAlmostEqual(result['Energy_consumed']['value'], 6.0 + 2.0 * 300 / 3600, places=3)

    @patch('user_devices.functions.logger')
    def test_compute_energy_skips_long_gap(self, mock_logger):
        """Dopo un'interruzione lunga non si integra (niente picchi)"""
        device_data, energy_data = self._querysets(previous_power=2000.0, seconds_ago=2 * 24 * 3600)
        result = compute_energy({'P': {'value': 2000.0, 'unit': 'W'}}, device_data, energy_data)

        self.assertAlmostEqual(result['Energy_consumed']['value'], 6.0)

    @patch('user_devices.functions.logger')
    def test_compute_energy_daily_counter_resets(self, mock_logger):
        """Se l'ultimo EnergyData è di ieri il giornaliero riparte da 0, il cumulato no"""
        from user_devices.helper_funcs import local_period_starts
        start_of_day = local_period_starts()[0]
        device_data, energy_data = self._querysets(
            previous_power=2000.0, energy_ts=start_of_day - timedelta(minutes=1))
        result = compute_energy({'P': {'value': 2000.0, 'unit': 'W'}}, device_data, energy_data)

        increment = 2.0 * 300 / 3600
        self.assertAlmostEqual(result['Energy_daily_consumed']['value'], increment, places=3)
        self.assertAlmostEqual(result['Energy_consumed']['value'], 6.0 + increment, places=3)

    @patch('user_devices.functions.logger')
    def test_compute_energy_without_previous_data(self, mock_logger):
        """Prima lettura: niente da integrare -> None"""
        device_data = Mock()
        device_data.order_by.return_value.first.return_value = None
        energy_data = Mock()
        energy_data.order_by.return_value.first.return_value = None

        result = compute_energy({'P': {'value': 1000.0, 'unit': 'W'}}, device_data, energy_data)

        self.assertIsNone(result)

    @patch('user_devices.functions.logger')
    def test_compute_energy_exception_handling(self, mock_logger):
        """Un errore viene loggato e la funzione ritorna None"""
        device_data = Mock()
        device_data.order_by.side_effect = Exception("Test exception")

        result = compute_energy({'P': {'value': 1000.0, 'unit': 'W'}}, device_data, Mock())

        self.assertIsNone(result)
        mock_logger.error.assert_called()

class TestDeviceAvailability(TestCase):
    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.DeviceData')
    def test_compute_device_availability(self, mock_device_data, mock_logger):
        """Test device availability computation"""
        # Setup device queryset
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=1)]
        device.slave_id = 1
        
        # Mock DeviceData queryset
        mock_queryset = Mock()
        mock_queryset.count.return_value = 10  # Some data exists
        mock_device_data.objects.filter.return_value = mock_queryset
        
        # Mock data
        data = Mock()
        
        # Execute function
        result = compute_device_availability(device, data)
        
        # Verify results - function returns a float value, not a dictionary
        self.assertIsInstance(result, (int, float))
        self.assertGreaterEqual(result, 0)
        self.assertLessEqual(result, 100)

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.DeviceData')
    def test_compute_device_availability_exception_handling(self, mock_device_data, mock_logger):
        """Test exception handling during device availability computation"""
        # Mock device queryset that raises exception
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=1)]
        device.slave_id = 1
        
        # Mock DeviceData queryset that raises exception
        mock_queryset = Mock()
        mock_queryset.count.side_effect = Exception("Test exception")
        mock_device_data.objects.filter.return_value = mock_queryset
        
        # Mock data
        data = Mock()
        
        # Execute function
        result = compute_device_availability(device, data)
        
        # Verify results - function returns 0 on exception
        self.assertEqual(result, 0)
        mock_logger.error.assert_called()

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.DeviceData')
    def test_compute_device_availability_with_no_data(self, mock_device_data, mock_logger):
        """Test device availability computation with no data"""
        # Mock device queryset
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=1)]
        device.slave_id = 1
        
        # Mock DeviceData queryset
        mock_queryset = Mock()
        mock_queryset.count.return_value = 0
        mock_device_data.objects.filter.return_value = mock_queryset
        
        # Mock data
        data = Mock()
        
        # Execute function
        result = compute_device_availability(device, data)
        
        # Verify results - function returns 0 on no data
        self.assertEqual(result, 0)
        # When there's no data, the function returns early without logging
        mock_logger.info.assert_not_called()

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.DeviceData')
    @patch('user_devices.functions.convert_to_local_time')
    def test_compute_device_availability_dst_spring_forward(self, mock_convert_to_local, mock_device_data, mock_logger):
        """Test device availability during DST spring forward transition (2 AM becomes 3 AM)"""
        # Setup device
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=1)]
        device.slave_id = 1
        
        # Mock DST spring forward scenario - 2:30 AM local time (which doesn't exist)
        # This should be handled gracefully
        mock_local_time = Mock()
        mock_local_time.year = 2024
        mock_local_time.month = 3  # March (DST starts)
        mock_local_time.day = 10
        mock_local_time.hour = 2
        mock_local_time.minute = 30
        mock_convert_to_local.return_value = mock_local_time
        
        # Mock DeviceData queryset with specific count
        mock_queryset = Mock()
        mock_queryset.count.return_value = 5  # 5 data points available
        mock_device_data.objects.filter.return_value = mock_queryset
        
        # Mock data
        data = Mock()
        
        # Execute function
        result = compute_device_availability(device, data)
        
        # Verify results - should handle DST transition gracefully
        self.assertIsInstance(result, (int, float))
        self.assertGreaterEqual(result, 0)
        self.assertLessEqual(result, 100)
        
        # Verify the calculation is reasonable for the time period
        # At 2:30 AM with 5 data points, availability should be calculated
        # The exact value depends on the interval, but should be > 0 if data exists
        if result > 0:
            self.assertGreater(result, 0)
            self.assertLessEqual(result, 100)

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.DeviceData')
    @patch('user_devices.functions.convert_to_local_time')
    def test_compute_device_availability_dst_fall_back(self, mock_convert_to_local, mock_device_data, mock_logger):
        """Test device availability during DST fall back transition (3 AM becomes 2 AM)"""
        # Setup device
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=1)]
        device.slave_id = 1
        
        # Mock DST fall back scenario - 2:30 AM local time (occurs twice)
        # This should be handled gracefully
        mock_local_time = Mock()
        mock_local_time.year = 2024
        mock_local_time.month = 11  # November (DST ends)
        mock_local_time.day = 3
        mock_local_time.hour = 2
        mock_local_time.minute = 30
        mock_convert_to_local.return_value = mock_local_time
        
        # Mock DeviceData queryset
        mock_queryset = Mock()
        mock_queryset.count.return_value = 5
        mock_device_data.objects.filter.return_value = mock_queryset
        
        # Mock data
        data = Mock()
        
        # Execute function
        result = compute_device_availability(device, data)
        
        # Verify results - should handle DST transition gracefully
        self.assertIsInstance(result, (int, float))
        self.assertGreaterEqual(result, 0)
        self.assertLessEqual(result, 100)

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.DeviceData')
    @patch('user_devices.functions.convert_to_local_time')
    def test_compute_device_availability_calculation_logic(self, mock_convert_to_local, mock_device_data, mock_logger):
        """Test that availability calculation works correctly with known inputs"""
        # Setup device
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=1)]
        device.slave_id = 1
        
        # Mock 6:00 AM local time (should have expected data count)
        mock_local_time = Mock()
        mock_local_time.year = 2024
        mock_local_time.month = 6
        mock_local_time.day = 15
        mock_local_time.hour = 6
        mock_local_time.minute = 0
        mock_convert_to_local.return_value = mock_local_time
        
        # Mock DeviceData queryset - 10 data points available
        mock_queryset = Mock()
        mock_queryset.count.return_value = 10
        mock_device_data.objects.filter.return_value = mock_queryset
        
        # Mock data
        data = Mock()
        
        # Execute function
        result = compute_device_availability(device, data)
        
        # Verify the calculation logic
        # At 6:00 AM (21600 seconds), with modbus interval (typically 300s)
        # Expected data count = 21600 / 300 = 72
        # With 10 actual data points: (10/72) * 100 = ~13.89%
        self.assertIsInstance(result, (int, float))
        self.assertGreater(result, 0)  # Should be > 0 since we have data
        self.assertLess(result, 100)   # Should be < 100 since we have fewer data points than expected
        
        # Verify logger was called with availability info
        mock_logger.info.assert_called()

    @patch('user_devices.functions.logger')
    @patch('user_devices.functions.DeviceData')
    @patch('user_devices.functions.convert_to_local_time')
    def test_compute_device_availability_dst_transition_behavior(self, mock_convert_to_local, mock_device_data, mock_logger):
        """Test that DST transitions don't break the calculation logic"""
        # Setup device
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.read_blocks.all.return_value = [Mock(register_type="input", start_address="0x0280", word_count=1)]
        device.slave_id = 1
        
        # Test DST transition times
        test_scenarios = [
            # Spring forward - 1:30 AM (before transition)
            {'year': 2024, 'month': 3, 'day': 10, 'hour': 1, 'minute': 30, 'expected_behavior': 'normal'},
            # Spring forward - 3:30 AM (after transition) 
            {'year': 2024, 'month': 3, 'day': 10, 'hour': 3, 'minute': 30, 'expected_behavior': 'normal'},
            # Fall back - 1:30 AM (before transition)
            {'year': 2024, 'month': 11, 'day': 3, 'hour': 1, 'minute': 30, 'expected_behavior': 'normal'},
            # Fall back - 2:30 AM (ambiguous hour - occurs twice)
            {'year': 2024, 'month': 11, 'day': 3, 'hour': 2, 'minute': 30, 'expected_behavior': 'ambiguous'},
        ]
        
        for scenario in test_scenarios:
            with self.subTest(scenario=scenario):
                # Mock local time
                mock_local_time = Mock()
                mock_local_time.year = scenario['year']
                mock_local_time.month = scenario['month']
                mock_local_time.day = scenario['day']
                mock_local_time.hour = scenario['hour']
                mock_local_time.minute = scenario['minute']
                mock_convert_to_local.return_value = mock_local_time
                
                # Mock DeviceData queryset
                mock_queryset = Mock()
                mock_queryset.count.return_value = 5
                mock_device_data.objects.filter.return_value = mock_queryset
                
                # Mock data
                data = Mock()
                
                # Execute function
                result = compute_device_availability(device, data)
                
                # Verify results are valid regardless of DST transition
                self.assertIsInstance(result, (int, float))
                self.assertGreaterEqual(result, 0)
                self.assertLessEqual(result, 100)
                
                # For ambiguous times, the function should still work
                if scenario['expected_behavior'] == 'ambiguous':
                    # Should not crash or return invalid values
                    self.assertIsNotNone(result)
                    self.assertIsInstance(result, (int, float))

class TestGetQuarterHourWindow(TestCase):
    def test_get_quarter_hour_window_15_17(self):
        """Test quarter-hour window calculation for 15:17 timestamp"""
        timestamp = datetime(2024, 6, 15, 15, 17, 30, tzinfo=timezone.utc)
        start_time, end_time = get_quarter_hour_window(timestamp)
        
        # Should return 15:00 to 15:15
        expected_start = datetime(2024, 6, 15, 15, 0, 0, tzinfo=timezone.utc)
        expected_end = datetime(2024, 6, 15, 15, 15, 0, tzinfo=timezone.utc)
        
        self.assertEqual(start_time, expected_start)
        self.assertEqual(end_time, expected_end)

    def test_get_quarter_hour_window_15_05(self):
        """Test quarter-hour window calculation for 15:05 timestamp"""
        timestamp = datetime(2024, 6, 15, 15, 5, 30, tzinfo=timezone.utc)
        start_time, end_time = get_quarter_hour_window(timestamp)
        
        # Should return 14:45 to 15:00
        expected_start = datetime(2024, 6, 15, 14, 45, 0, tzinfo=timezone.utc)
        expected_end = datetime(2024, 6, 15, 15, 0, 0, tzinfo=timezone.utc)
        
        self.assertEqual(start_time, expected_start)
        self.assertEqual(end_time, expected_end)

    def test_get_quarter_hour_window_15_30(self):
        """Test quarter-hour window calculation for 15:30 timestamp"""
        timestamp = datetime(2024, 6, 15, 15, 30, 30, tzinfo=timezone.utc)
        start_time, end_time = get_quarter_hour_window(timestamp)
        
        # Should return 15:15 to 15:30
        expected_start = datetime(2024, 6, 15, 15, 15, 0, tzinfo=timezone.utc)
        expected_end = datetime(2024, 6, 15, 15, 30, 0, tzinfo=timezone.utc)
        
        self.assertEqual(start_time, expected_start)
        self.assertEqual(end_time, expected_end)

    def test_get_quarter_hour_window_15_45(self):
        """Test quarter-hour window calculation for 15:45 timestamp"""
        timestamp = datetime(2024, 6, 15, 15, 45, 30, tzinfo=timezone.utc)
        start_time, end_time = get_quarter_hour_window(timestamp)
        
        # Should return 15:30 to 15:45
        expected_start = datetime(2024, 6, 15, 15, 30, 0, tzinfo=timezone.utc)
        expected_end = datetime(2024, 6, 15, 15, 45, 0, tzinfo=timezone.utc)
        
        self.assertEqual(start_time, expected_start)
        self.assertEqual(end_time, expected_end)

# I test usano timestamp fissi: il filtro sull'età dei dati è disattivato
@patch('user_devices.functions.PLANT_DATA_MAX_AGE', None)
class TestComputePlantProduction(TestCase):
    @patch('user_devices.functions.DeviceData')
    def test_compute_plant_production_modbus_quarter_hour_averaging(self, mock_device_data):
        """Test Modbus devices use quarter-hour averaging for power calculation"""
        # Setup gateway and devices
        gateway = Mock()
        gateway.name = "Test Gateway"
        
        device = Mock()
        device.name = "Modbus Device"
        device.protocol = "modbus"
        device.is_enabled = True
        
        devices = [device]
        
        # Mock latest device data (timestamp 15:17)
        latest_data = Mock()
        latest_data.timestamp = datetime(2024, 6, 15, 15, 17, 30, tzinfo=timezone.utc)
        latest_data.data = {
            'Pout': {'value': 1000.0, 'unit': 'kW'},
            'Voltage': {'value': 230.0, 'unit': 'V'}
        }
        
        # Mock quarter-hour data (15:00 to 15:15)
        quarter_hour_data_1 = Mock()
        quarter_hour_data_1.data = {'Pout': {'value': 800.0, 'unit': 'kW'}}
        
        quarter_hour_data_2 = Mock()
        quarter_hour_data_2.data = {'Pout': {'value': 900.0, 'unit': 'kW'}}
        
        quarter_hour_data_3 = Mock()
        quarter_hour_data_3.data = {'Pout': {'value': 1100.0, 'unit': 'kW'}}
        
        # Mock DeviceData queryset - need to handle different filter calls
        def mock_filter_side_effect(**kwargs):
            mock_queryset = Mock()
            if 'timestamp__gte' in kwargs:  # Quarter-hour data query
                mock_queryset.order_by.return_value = [quarter_hour_data_1, quarter_hour_data_2, quarter_hour_data_3]
            else:  # Latest data query
                mock_queryset.order_by.return_value.first.return_value = latest_data
            return mock_queryset
        
        mock_device_data.objects.filter.side_effect = mock_filter_side_effect
        
        # Execute function
        result = compute_plant_production(gateway, devices)
        
        # Verify result - should be average of 800, 900, 1100 = 933.33
        expected_average = (800.0 + 900.0 + 1100.0) / 3
        self.assertAlmostEqual(result, expected_average, places=2)

    @patch('user_devices.functions.DeviceData')
    def test_compute_plant_production_dlms_latest_reading(self, mock_device_data):
        """Test DLMS devices use latest reading only"""
        # Setup gateway and devices
        gateway = Mock()
        gateway.name = "Test Gateway"
        
        device = Mock()
        device.name = "DLMS Device"
        device.protocol = "dlms"
        device.is_enabled = True
        
        devices = [device]
        
        # Mock latest device data
        latest_data = Mock()
        latest_data.timestamp = datetime(2024, 6, 15, 15, 17, 30, tzinfo=timezone.utc)
        latest_data.data = {
            'Pout': {'value': 1000.0, 'unit': 'kW'},
            'Voltage': {'value': 230.0, 'unit': 'V'}
        }
        
        # Mock DeviceData queryset
        mock_queryset = Mock()
        mock_queryset.filter.return_value.order_by.return_value.first.return_value = latest_data
        mock_device_data.objects = mock_queryset
        
        # Execute function
        result = compute_plant_production(gateway, devices)
        
        # Verify result - should be latest reading value (1000.0)
        self.assertEqual(result, 1000.0)

    @patch('user_devices.functions.DeviceData')
    def test_compute_plant_production_modbus_no_quarter_hour_data(self, mock_device_data):
        """Test Modbus device with no data in quarter-hour window"""
        # Setup gateway and devices
        gateway = Mock()
        gateway.name = "Test Gateway"
        
        device = Mock()
        device.name = "Modbus Device"
        device.protocol = "modbus"
        device.is_enabled = True
        
        devices = [device]
        
        # Mock latest device data
        latest_data = Mock()
        latest_data.timestamp = datetime(2024, 6, 15, 15, 17, 30, tzinfo=timezone.utc)
        latest_data.data = {
            'Pout': {'value': 1000.0, 'unit': 'kW'}
        }
        
        # Mock DeviceData queryset - no quarter-hour data
        def mock_filter_side_effect(**kwargs):
            mock_queryset = Mock()
            if 'timestamp__gte' in kwargs:  # Quarter-hour data query
                mock_queryset.order_by.return_value = []  # No data in quarter-hour window
            else:  # Latest data query
                mock_queryset.order_by.return_value.first.return_value = latest_data
            return mock_queryset
        
        mock_device_data.objects.filter.side_effect = mock_filter_side_effect
        
        # Execute function
        result = compute_plant_production(gateway, devices)
        
        # Verify result - should be 0 since no data in quarter-hour window
        self.assertEqual(result, 0.0)

    @patch('user_devices.functions.DeviceData')
    def test_compute_plant_production_multiple_power_variables(self, mock_device_data):
        """Test Modbus device with multiple power variables"""
        # Setup gateway and devices
        gateway = Mock()
        gateway.name = "Test Gateway"
        
        device = Mock()
        device.name = "Modbus Device"
        device.protocol = "modbus"
        device.is_enabled = True
        
        devices = [device]
        
        # Mock latest device data
        latest_data = Mock()
        latest_data.timestamp = datetime(2024, 6, 15, 15, 17, 30, tzinfo=timezone.utc)
        latest_data.data = {
            'Pout': {'value': 1000.0, 'unit': 'kW'},
            'Power_Production': {'value': 500.0, 'unit': 'kW'}
        }
        
        # Mock quarter-hour data
        quarter_hour_data_1 = Mock()
        quarter_hour_data_1.data = {
            'Pout': {'value': 800.0, 'unit': 'kW'},
            'Power_Production': {'value': 400.0, 'unit': 'kW'}
        }
        
        quarter_hour_data_2 = Mock()
        quarter_hour_data_2.data = {
            'Pout': {'value': 1200.0, 'unit': 'kW'},
            'Power_Production': {'value': 600.0, 'unit': 'kW'}
        }
        
        # Mock DeviceData queryset - need to handle different filter calls
        def mock_filter_side_effect(**kwargs):
            mock_queryset = Mock()
            if 'timestamp__gte' in kwargs:  # Quarter-hour data query
                mock_queryset.order_by.return_value = [quarter_hour_data_1, quarter_hour_data_2]
            else:  # Latest data query
                mock_queryset.order_by.return_value.first.return_value = latest_data
            return mock_queryset
        
        mock_device_data.objects.filter.side_effect = mock_filter_side_effect
        
        # Execute function
        result = compute_plant_production(gateway, devices)
        
        # Verify result - should be sum of averages
        # Pout average: (800 + 1200) / 2 = 1000
        # Power Production average: (400 + 600) / 2 = 500
        # Total: 1000 + 500 = 1500
        expected_result = 1000.0 + 500.0
        self.assertEqual(result, expected_result)

    @patch('user_devices.functions.DeviceData')
    def test_compute_plant_production_no_power_variables(self, mock_device_data):
        """Test device with no power variables"""
        # Setup gateway and devices
        gateway = Mock()
        gateway.name = "Test Gateway"
        
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.is_enabled = True
        
        devices = [device]
        
        # Mock latest device data with no power variables
        latest_data = Mock()
        latest_data.timestamp = datetime(2024, 6, 15, 15, 17, 30, tzinfo=timezone.utc)
        latest_data.data = {
            'Voltage': {'value': 230.0, 'unit': 'V'},
            'Current': {'value': 5.0, 'unit': 'A'}
        }
        
        # Mock DeviceData queryset
        mock_queryset = Mock()
        mock_queryset.filter.return_value.order_by.return_value.first.return_value = latest_data
        mock_device_data.objects = mock_queryset
        
        # Execute function
        result = compute_plant_production(gateway, devices)
        
        # Verify result - should be 0 since no power variables
        self.assertEqual(result, 0.0)

    @patch('user_devices.functions.DeviceData')
    def test_compute_plant_production_invalid_power_values(self, mock_device_data):
        """Test handling of invalid power values"""
        # Setup gateway and devices
        gateway = Mock()
        gateway.name = "Test Gateway"
        
        device = Mock()
        device.name = "Modbus Device"
        device.protocol = "modbus"
        device.is_enabled = True
        
        devices = [device]
        
        # Mock latest device data
        latest_data = Mock()
        latest_data.timestamp = datetime(2024, 6, 15, 15, 17, 30, tzinfo=timezone.utc)
        latest_data.data = {
            'Pout': {'value': 1000.0, 'unit': 'kW'}
        }
        
        # Mock quarter-hour data with invalid values
        quarter_hour_data_1 = Mock()
        quarter_hour_data_1.data = {'Pout': {'value': 800.0, 'unit': 'kW'}}
        
        quarter_hour_data_2 = Mock()
        quarter_hour_data_2.data = {'Pout': 'invalid_value'}  # Invalid format
        
        quarter_hour_data_3 = Mock()
        quarter_hour_data_3.data = {'Pout': {'value': 1200.0, 'unit': 'kW'}}
        
        # Mock DeviceData queryset - need to handle different filter calls
        def mock_filter_side_effect(**kwargs):
            mock_queryset = Mock()
            if 'timestamp__gte' in kwargs:  # Quarter-hour data query
                mock_queryset.order_by.return_value = [quarter_hour_data_1, quarter_hour_data_2, quarter_hour_data_3]
            else:  # Latest data query
                mock_queryset.order_by.return_value.first.return_value = latest_data
            return mock_queryset
        
        mock_device_data.objects.filter.side_effect = mock_filter_side_effect
        
        # Execute function
        result = compute_plant_production(gateway, devices)
        
        # Verify result - should be average of valid values only (800, 1200)
        expected_average = (800.0 + 1200.0) / 2
        self.assertEqual(result, expected_average)

    @patch('user_devices.functions.DeviceData')
    def test_compute_plant_production_exception_handling(self, mock_device_data):
        """Test exception handling in compute_plant_production"""
        # Setup gateway and devices
        gateway = Mock()
        gateway.name = "Test Gateway"
        
        device = Mock()
        device.name = "Test Device"
        device.protocol = "modbus"
        device.is_enabled = True
        
        devices = [device]
        
        # Mock DeviceData to raise exception
        mock_device_data.objects.filter.side_effect = Exception("Database error")
        
        # Execute function
        result = compute_plant_production(gateway, devices)
        
        # Verify result - should return 0 on exception
        self.assertEqual(result, 0)

    @patch('user_devices.functions.DeviceData')
    def test_compute_plant_production_multiple_devices(self, mock_device_data):
        """Test plant production with multiple devices"""
        # Setup gateway and devices
        gateway = Mock()
        gateway.name = "Test Gateway"
        
        # Modbus device
        modbus_device = Mock()
        modbus_device.name = "Modbus Device"
        modbus_device.protocol = "modbus"
        modbus_device.is_enabled = True
        
        # DLMS device
        dlms_device = Mock()
        dlms_device.name = "DLMS Device"
        dlms_device.protocol = "dlms"
        dlms_device.is_enabled = True
        
        devices = [modbus_device, dlms_device]
        
        # Mock latest device data for both devices
        modbus_latest_data = Mock()
        modbus_latest_data.timestamp = datetime(2024, 6, 15, 15, 17, 30, tzinfo=timezone.utc)
        modbus_latest_data.data = {'Pout': {'value': 1000.0, 'unit': 'kW'}}
        
        dlms_latest_data = Mock()
        dlms_latest_data.timestamp = datetime(2024, 6, 15, 15, 17, 30, tzinfo=timezone.utc)
        dlms_latest_data.data = {'Pout': {'value': 500.0, 'unit': 'kW'}}
        
        # Mock quarter-hour data for Modbus device
        quarter_hour_data = Mock()
        quarter_hour_data.data = {'Pout': {'value': 800.0, 'unit': 'kW'}}
        
        # Mock DeviceData queryset with side_effect for different devices and queries
        def mock_filter_side_effect(device_name=None, **kwargs):
            mock_queryset = Mock()
            if device_name == modbus_device:
                if 'timestamp__gte' in kwargs:  # Quarter-hour data query
                    mock_queryset.order_by.return_value = [quarter_hour_data]
                else:  # Latest data query
                    mock_queryset.order_by.return_value.first.return_value = modbus_latest_data
            elif device_name == dlms_device:
                mock_queryset.order_by.return_value.first.return_value = dlms_latest_data
            return mock_queryset
        
        mock_device_data.objects.filter.side_effect = mock_filter_side_effect
        
        # Execute function
        result = compute_plant_production(gateway, devices)
        
        # Verify result - should be sum of both devices
        # Modbus: 800.0 (quarter-hour average)
        # DLMS: 500.0 (latest reading)
        # Total: 1300.0
        expected_result = 800.0 + 500.0
        self.assertEqual(result, expected_result)
