"""
Test dei preset di mappatura Modbus e della lettura multi-blocco.

Lanciare con:
    docker compose exec web python manage.py test user_devices.tests.test_presets
"""

from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase

from user_devices.forms import DeviceForm
from user_devices.functions import (
    POWER_CONS_NAMES, POWER_NAMES, POWER_PROD_NAMES, compute_variables, map_variables, read_modbus_registers,
)
from user_devices.helper_funcs import evaluate_formula, sanitize_variable_name
from user_devices.models import ComputedVariable, Device, Gateway, ModbusMappingVariable, ModbusReadBlock
from user_devices.presets import apply_preset, load_presets, preset_choices


class PresetFilesTests(TestCase):
    """Ogni file JSON deve essere coerente: registri coperti dai blocchi, formule valide."""

    def test_presets_are_loaded(self):
        self.assertGreater(len(load_presets()), 50)

    def test_every_preset_is_consistent(self):
        for preset_id, preset in load_presets().items():
            with self.subTest(preset=preset_id):
                covered = set()
                for block in preset["blocks"]:
                    self.assertIn(block["register_type"], ("input", "holding"))
                    self.assertLessEqual(block["word_count"], 125)
                    start = int(block["start_address"], 16)
                    covered |= {(block["register_type"], a) for a in range(start, start + block["word_count"])}

                names = [v["name"] for v in preset["variables"] + preset["computed"]]
                self.assertEqual(len(names), len(set(names)), "nomi duplicati")

                for var in preset["variables"]:
                    self.assertIn(var["bit_length"], (16, 32, 64))
                    self.assertIn(var["endianness"], ("big", "little"))
                    float(var["conversion_factor"])
                    start = int(var["address"], 16)
                    for addr in range(start, start + var["bit_length"] // 16):
                        self.assertIn((var["register_type"], addr), covered, var["name"])

                # Ogni formula deve essere calcolabile con le variabili del preset
                known = {sanitize_variable_name(n): 1.0 for n in names}
                for comp in preset["computed"]:
                    evaluate_formula(comp["formula"], known)

                # L'alias Pout (kW) è l'unico nome che compute_energy riconosce
                reserved = set(POWER_NAMES + POWER_PROD_NAMES + POWER_CONS_NAMES) - {"Pout"}
                self.assertFalse(reserved & {sanitize_variable_name(n) for n in names})

    def test_choices_grouped_with_empty_option(self):
        choices = preset_choices()
        self.assertEqual(choices[0][0], "")
        self.assertEqual(choices[1][0], "Inverter")
        self.assertTrue(DeviceForm().fields["apply_preset"].choices)


FAKE_PRESET = {
    "id": "inverter/test_fake",
    "category": "inverter",
    "manufacturer": "Test",
    "model": "Fake",
    "blocks": [
        {"register_type": "input", "start_address": "0x0010", "word_count": 3},
        {"register_type": "holding", "start_address": "0x0100", "word_count": 2},
        {"register_type": "holding", "start_address": "0x0200", "word_count": 1},
    ],
    "variables": [
        {"name": "Voltage", "register_type": "input", "address": "0x0010", "unit": "V",
         "conversion_factor": "0.1", "offset": 0, "bit_length": 16, "is_signed": False, "endianness": "big"},
        {"name": "Temperature", "register_type": "input", "address": "0x0011", "unit": "°C",
         "conversion_factor": "0.1", "offset": 1000, "bit_length": 16, "is_signed": False, "endianness": "big"},
        {"name": "Output Power", "register_type": "holding", "address": "0x0100", "unit": "W",
         "conversion_factor": "1", "offset": 0, "bit_length": 32, "is_signed": True, "endianness": "little"},
        {"name": "Energy lo", "register_type": "input", "address": "0x0012", "unit": "kWh",
         "conversion_factor": "0.1", "offset": 0, "bit_length": 16, "is_signed": False, "endianness": "big"},
        {"name": "Energy hi", "register_type": "holding", "address": "0x0200", "unit": "kWh",
         "conversion_factor": "6553.6", "offset": 0, "bit_length": 16, "is_signed": False, "endianness": "big"},
    ],
    "computed": [
        {"name": "Energy", "unit": "kWh", "formula": "Energy_lo+Energy_hi"},
        {"name": "Pout", "unit": "kW", "formula": "max(0, Output_Power/1000)", "show_on_graph": True, "show_in_homepage": True},
    ],
}


class ApplyPresetTests(TestCase):
    def setUp(self):
        # Il post_save del Gateway parlerebbe con il vero mosquitto-admin
        admin_patcher = patch("user_devices.signals.admin_client")
        admin_patcher.start()
        self.addCleanup(admin_patcher.stop)
        self.gateway = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        self.device = Device.objects.create(name="inv", Gateway=self.gateway, slave_id=1, port=502)
        patcher = patch("user_devices.presets.load_presets", return_value={FAKE_PRESET["id"]: FAKE_PRESET})
        patcher.start()
        self.addCleanup(patcher.stop)
        apply_preset(self.device, FAKE_PRESET["id"])

    def test_creates_blocks_variables_and_computed(self):
        self.device.refresh_from_db()
        self.assertEqual(self.device.preset, FAKE_PRESET["id"])
        self.assertEqual(ModbusReadBlock.objects.filter(device=self.device).count(), 3)
        self.assertEqual(ModbusMappingVariable.objects.filter(device=self.device).count(), 5)
        power = ComputedVariable.objects.get(device=self.device, var_name="Pout")
        self.assertTrue(power.show_on_graph)

    def test_values_through_pipeline(self):
        base_values = {
            ("input", 0x10): 2301,                 # 230.1 V
            ("input", 0x11): 1250,                 # (1250 - 1000) * 0.1 = 25 °C
            ("input", 0x12): 10,                   # energy lo
            ("holding", 0x100): 0xFC18,            # word bassa di -1000
            ("holding", 0x101): 0xFFFF,            # word alta
            ("holding", 0x200): 1,                 # energy hi
        }
        mapped = map_variables(base_values, self.device)
        computed = compute_variables(mapped, self.device)

        self.assertEqual(mapped["Voltage"]["value"], 230.1)
        self.assertEqual(mapped["Temperature"]["value"], 25.0)
        self.assertEqual(mapped["Output_Power"]["value"], -1000.0)
        self.assertEqual(computed["Pout"]["value"], 0)  # mai negativa
        self.assertEqual(computed["Energy"]["value"], 6554.6)  # (10 + 65536) * 0.1

    def test_pout_in_kw(self):
        """Pout è la potenza prodotta in kW: compute_energy la integra come produzione."""
        mapped = map_variables({("holding", 0x100): 2500, ("holding", 0x101): 0}, self.device)
        computed = compute_variables(mapped, self.device)
        self.assertEqual(computed["Pout"], {"value": 2.5, "unit": "kW"})

    def test_same_address_different_register_type(self):
        """input 0x10 e holding 0x10 sono registri diversi."""
        mapped = map_variables({("holding", 0x10): 999}, self.device)
        self.assertNotIn("Voltage", mapped)  # registro mancante: variabile saltata

    def test_read_modbus_registers_reads_every_block(self):
        client = Mock()

        def response(address, count, device_id):
            r = Mock()
            r.isError.return_value = False
            r.registers = list(range(address, address + count))
            return r

        client.read_input_registers.side_effect = response
        client.read_holding_registers.side_effect = response

        with patch("user_devices.functions.time.sleep"):
            base_values = read_modbus_registers(self.device, client)

        self.assertEqual(base_values[("input", 0x10)], 0x10)
        self.assertEqual(base_values[("input", 0x12)], 0x12)
        self.assertEqual(base_values[("holding", 0x101)], 0x101)
        self.assertEqual(base_values[("holding", 0x200)], 0x200)
        self.assertEqual(len(base_values), 6)

    def test_export_telegraf_config_has_one_request_per_block(self):
        self.device.is_enabled = True
        self.device.save()
        out = StringIO()
        with patch("sys.stdout", out):
            call_command("export_telegraf_config", self.gateway.pk)
        config = out.getvalue()

        self.assertEqual(config.count("[[inputs.modbus.request]]"), 3)
        self.assertIn('name = "ir_0x0010"', config)
        self.assertIn('name = "hr_0x0101"', config)
        self.assertIn('name = "hr_0x0200"', config)
        self.assertNotIn("reg_0x", config)
