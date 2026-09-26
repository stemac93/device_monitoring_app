import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from user_devices import functions
from user_devices.models import Button, Device, DeviceData, Gateway, ModbusMappingVariable


def _previous(data, seconds_ago=300):
    record = Mock()
    record.timestamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    record.data = data
    qs = Mock()
    qs.order_by.return_value.first.return_value = record
    return qs


def _no_energy():
    qs = Mock()
    qs.order_by.return_value.first.return_value = None
    return qs


class EnergyNamesTests(TestCase):
    def test_modbus_pout_is_integrated_without_timestamp(self):
        """Pout/Pin su Modbus (senza timestamp DLMS) vengono integrati nel tempo."""
        device_data = _previous({"Pout": {"value": 3.0, "unit": "kW"}, "Pin": {"value": 1.0, "unit": "kW"}})
        result = functions.compute_energy(
            {"Pout": {"value": 3.0, "unit": "kW"}, "Pin": {"value": 1.0, "unit": "kW"}},
            device_data, _no_energy())
        self.assertAlmostEqual(result["Energy_produced"]["value"], 3.0 * 300 / 3600, places=3)
        self.assertAlmostEqual(result["Energy_consumed"]["value"], 1.0 * 300 / 3600, places=3)
        self.assertNotIn("timestamp", result)

    def test_dlms_split_power_uses_quarter_hour(self):
        result = functions.compute_energy(
            {"Pout": {"value": 4.0, "unit": "kW"}, "timestamp": "2026-07-01T10:15:00"},
            _previous({}), _no_energy())
        self.assertAlmostEqual(result["Energy_produced"]["value"], 1.0)
        self.assertEqual(result["timestamp"], "2026-07-01T10:15:00")

    def test_sanitized_names_with_spaces_recognized(self):
        """'Potenza in uscita' è salvata come 'Potenza_in_uscita' e va riconosciuta."""
        device_data = _previous({"Potenza_in_uscita": {"value": 2.0, "unit": "kW"}})
        result = functions.compute_energy(
            {"Potenza_in_uscita": {"value": 2.0, "unit": "kW"}}, device_data, _no_energy())
        self.assertIsNotNone(result)
        self.assertGreater(result["Energy_produced"]["value"], 0)


class PlantDataAgeTests(TestCase):
    def setUp(self):
        self.gw = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        self.dev = Device.objects.create(name="inv", Gateway=self.gw, is_enabled=True, protocol="dlms")
        self.dd = DeviceData.objects.create(Gateway=self.gw, device_name=self.dev,
                                            data={"Pout": {"value": 5.0}, "Rad": {"value": 800.0}})

    def test_recent_data_counts(self):
        self.assertEqual(functions.compute_plant_production(self.gw, [self.dev]), 5.0)
        self.assertEqual(functions.find_radiance_value([self.dev]), 800.0)

    def test_stale_data_ignored(self):
        DeviceData.objects.filter(pk=self.dd.pk).update(timestamp=datetime.now(timezone.utc) - timedelta(hours=3))
        self.assertEqual(functions.compute_plant_production(self.gw, [self.dev]), 0)
        self.assertIsNone(functions.find_radiance_value([self.dev]))


class DuplicateCheckTests(TestCase):
    def test_unparsable_timestamp_is_stored_not_dropped(self):
        gw = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        dev = Device.objects.create(name="m", Gateway=gw, protocol="dlms")
        DeviceData.objects.create(Gateway=gw, device_name=dev, data={"timestamp": "not-a-date"})
        self.assertFalse(functions.is_device_data_already_stored(dev, {"timestamp": "2026-01-01T00:00:00"}))


class CheckAllDevicesTests(TestCase):
    def test_one_task_per_gateway_pk_even_with_same_ip(self):
        from user_devices import tasks
        g1 = Gateway.objects.create(name="a", ip_address="10.0.0.1")
        g2 = Gateway.objects.create(name="b", ip_address="10.0.0.1")
        with patch.object(tasks, "group") as group, patch.object(tasks, "scan_and_read_devices") as scan:
            tasks.check_all_devices()
            list(group.call_args[0][0])
        self.assertEqual(sorted(c.args[0] for c in scan.s.call_args_list), sorted([g1.pk, g2.pk]))


class WebTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="pw")
        self.gw = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        self.gw.user.add(self.user)
        self.client.login(username="u", password="pw")

    def _download(self, **data):
        base = {"data_type": f"gateway_{self.gw.pk}", "start_date": "2026-01-01", "end_date": "2026-01-10"}
        base.update(data)
        return self.client.post(reverse("download_data"), base, follow=True)

    def test_tampered_data_type_shows_error(self):
        response = self._download(data_type="gateway_abc")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid data type selected.")  # il messaggio ora si vede

    def test_range_limit_is_30_days(self):
        response = self._download(start_date="2026-01-01", end_date="2026-01-31")  # 31 giorni
        self.assertContains(response, "Date range cannot exceed 30 days.")

    def test_csv_filename_is_safe(self):
        self.gw.name = 'Impianto "Sud" àè'
        self.gw.save()
        from user_devices.models import GatewayData
        GatewayData.objects.create(Gateway=self.gw, data={"p": {"value": 1}}, timestamp="2026-01-05T10:00:00Z")
        response = self.client.post(reverse("download_data"), {
            "data_type": f"gateway_{self.gw.pk}", "start_date": "2026-01-01", "end_date": "2026-01-10"})
        self.assertEqual(response["Content-Disposition"],
                         'attachment; filename="Gateway_impianto-sud-ae_2026-01-01_to_2026-01-10.csv"')

    def test_device_name_with_slash_and_quoted_label(self):
        dev = Device.objects.create(name="Inv 1/A", Gateway=self.gw, is_enabled=True)
        ModbusMappingVariable.objects.create(device=dev, var_name='P "tot" \\ </script>', address="0x0",
                                             show_on_graph=True, show_in_homepage=True)
        DeviceData.objects.create(Gateway=self.gw, device_name=dev, data={})
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        response = self.client.get(reverse("device_detail", args=[dev.name]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "</script> ")  # escape di json_script
        self.assertContains(response, 'id="chart-data"')

    def test_button_str_without_gateway(self):
        self.assertEqual(str(Button(label="x", pin_number=1)), "x (no gateway)")
