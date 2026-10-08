import io
import tarfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from user_devices.helper_funcs import convert_value
from user_devices.models import (
    Device, DeviceData, EnergyData, Gateway, GatewayData, GatewayMqttCredentials, ModbusMappingVariable,
    ModbusReadBlock,
)


class UserSyncTests(TestCase):
    def setUp(self):
        self.gw = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        self.dev = Device.objects.create(name="d", Gateway=self.gw)
        self.dd = DeviceData.objects.create(Gateway=self.gw, device_name=self.dev, data={})
        self.ed = EnergyData.objects.create(Gateway=self.gw, device_name=self.dev, data={})
        self.gd = GatewayData.objects.create(Gateway=self.gw, data={}, timestamp="2026-01-01T00:00:00Z")
        self.u1 = User.objects.create_user("u1")
        self.u2 = User.objects.create_user("u2")

    def _users(self, obj):
        return set(obj.user.values_list("username", flat=True))

    def test_add_and_remove_propagate_to_all_models(self):
        self.gw.user.add(self.u1, self.u2)
        for obj in (self.dev, self.dd, self.ed, self.gd):
            self.assertEqual(self._users(obj), {"u1", "u2"}, obj)
        self.gw.user.remove(self.u1)
        for obj in (self.dev, self.dd, self.ed, self.gd):
            self.assertEqual(self._users(obj), {"u2"}, obj)

    def test_reverse_side_and_clear(self):
        self.u1.user_gateway.add(self.gw)  # dal lato utente
        self.assertEqual(self._users(self.dd), {"u1"})
        self.u1.user_gateway.clear()
        self.assertEqual(self._users(self.dd), set())
        self.gw.user.add(self.u2)
        self.gw.user.clear()
        self.assertEqual(self._users(self.ed), set())

    def test_new_device_inherits_gateway_users(self):
        self.gw.user.add(self.u1)
        new_dev = Device.objects.create(name="new", Gateway=self.gw)
        self.assertEqual(self._users(new_dev), {"u1"})


class GatewayAdminTests(TestCase):
    def setUp(self):
        User.objects.create_superuser(username="admin", password="pw", email="a@example.com")
        self.client.login(username="admin", password="pw")
        self.gw = Gateway.objects.create(name="My GW/1", ip_address="10.0.0.1")
        # Le credenziali le crea il signal post_save: fisso una password nota
        GatewayMqttCredentials.objects.filter(gateway=self.gw).update(password_plaintext="secret-pw")

    def test_search_does_not_crash(self):
        for name in ("gateway", "device", "devicedata", "energydata", "gatewaydata"):
            response = self.client.get(reverse(f"admin:user_devices_{name}_changelist"), {"q": "x"})
            self.assertEqual(response.status_code, 200, name)

    def test_clone_device(self):
        dev = Device.objects.create(name="inv", Gateway=self.gw)
        ModbusReadBlock.objects.create(device=dev, register_type="holding", start_address="0x0010", word_count=12)
        ModbusMappingVariable.objects.create(device=dev, var_name="P", address="0x0", endianness="little", offset=5)
        response = self.client.post(reverse("admin:user_devices_device_changelist"), {
            "action": "clone_device", "_selected_action": [dev.pk]})
        self.assertEqual(response.status_code, 302)
        clone = Device.objects.get(name="inv Copy")
        block = clone.read_blocks.get()
        self.assertEqual((block.register_type, block.start_address, block.word_count), ("holding", "0x0010", 12))
        var = clone.modbus_variables.get()
        self.assertEqual((var.endianness, var.offset), ("little", 5))

    def test_credentials_admin_never_shows_password(self):
        cred = self.gw.mqtt_credentials
        response = self.client.get(reverse("admin:user_devices_gatewaymqttcredentials_change", args=[cred.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "secret-pw")

    def test_bundle_get_rejected(self):
        response = self.client.get(reverse("gateway_mqtt_bundle", args=[self.gw.pk]))
        self.assertEqual(response.status_code, 405)
        self.gw.mqtt_credentials.refresh_from_db()
        self.assertFalse(self.gw.mqtt_credentials.password_revealed)

    def test_bundle_without_devices_redirects_and_keeps_password(self):
        response = self.client.post(reverse("gateway_mqtt_bundle", args=[self.gw.pk]))
        self.assertRedirects(response, reverse("admin:user_devices_gateway_change", args=[self.gw.pk]),
                             fetch_redirect_response=False)
        self.gw.mqtt_credentials.refresh_from_db()
        self.assertEqual(self.gw.mqtt_credentials.password_plaintext, "secret-pw")

    def test_bundle_download_once(self):
        dev = Device.objects.create(name="inv", Gateway=self.gw, is_enabled=True, protocol="modbus",
                                    slave_id=1, port=503)
        ModbusReadBlock.objects.create(device=dev, start_address="0x0", word_count=2)
        with patch.dict("os.environ", {"MQTT_PUBLIC_ENDPOINT": "ssl://10.8.0.1:8883"}), \
             patch("user_devices.admin_mqtt.Path.exists", return_value=True), \
             patch("user_devices.admin_mqtt.Path.read_bytes", return_value=b"CA"):
            url = reverse("gateway_mqtt_bundle", args=[self.gw.pk])
            response = self.client.post(url)
            second = self.client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertIn('filename="gateway-%d-my-gw1-bundle.tar.gz"' % self.gw.pk, response["Content-Disposition"])
        with tarfile.open(fileobj=io.BytesIO(response.content)) as tf:
            env = tf.extractfile("telegraf.env").read().decode()
            conf = tf.extractfile("telegraf.conf").read().decode()
        self.assertIn("MQTT_PASSWORD=secret-pw", env)
        self.assertIn('namepass = ["modbus"]', conf)
        self.assertEqual(second.status_code, 302)  # password già consegnata
        self.gw.mqtt_credentials.refresh_from_db()
        self.assertEqual(self.gw.mqtt_credentials.password_plaintext, "")


class ConvertValueTests(TestCase):
    def test_conversion_factors(self):
        self.assertEqual(convert_value(100, "0,1"), 10.0)
        self.assertEqual(convert_value(100, "1/4"), 25.0)
        self.assertEqual(convert_value(100, None), 100.0)
        self.assertIsNone(convert_value(100, "abc"))
