from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from user_devices.commands import execute_ssh_command, set_pin_status
from user_devices.forms import DeviceForm
from user_devices.helper_funcs import evaluate_formula
from user_devices.models import Device, EnergyData, Gateway, GatewayMqttCredentials


class EvaluateFormulaTests(TestCase):
    def test_arithmetic(self):
        v = {"Pin": 10.0, "Pout": 4.0, "V": 230.0, "I": 2.0}
        self.assertEqual(evaluate_formula("Pin - Pout", v), 6.0)
        self.assertEqual(evaluate_formula("(V * I) / 1000", v), 0.46)
        self.assertEqual(evaluate_formula("-Pout + abs(-2) + max(Pin, 1)", v), 8.0)
        self.assertEqual(evaluate_formula("Pout ^ 2", v), 16.0)  # "^" = potenza come con sympify
        self.assertEqual(evaluate_formula("sqrt(Pout)", v), 2.0)

    def test_rejects_code_and_bad_input(self):
        for formula in ("__import__('os').system('id')", "Pin.__class__", "[1, 2]", "lambda: 1",
                        "Pin if Pin else 0", "2 ** 100000", "Missing * 2", "Pin -", ""):
            with self.assertRaises(ValueError, msg=formula):
                evaluate_formula(formula, {"Pin": 1.0})
        with self.assertRaises(ZeroDivisionError):
            evaluate_formula("Pin / 0", {"Pin": 1.0})


class DeviceFormTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.gw = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        self.user = User.objects.create_user("u")

    def _form(self, **overrides):
        data = {"name": "d", "Gateway": self.gw.pk, "user": [self.user.pk],
                "protocol": "modbus", "slave_id": 1, "register_type": "input",
                "start_address": "0x0000", "word_count": 2, "port": 502, "availability": 0,
                "daily_production": 0, "daily_consumption": 0}
        data.update(overrides)
        return DeviceForm(data=data)

    def test_valid_modbus_with_zero_address(self):
        self.assertTrue(self._form().is_valid(), self._form().errors)

    def test_invalid_modbus_fields(self):
        form = self._form(slave_id=-1, start_address="zz", word_count=0)
        self.assertFalse(form.is_valid())
        self.assertIn("slave_id", form.errors)
        self.assertIn("start_address", form.errors)
        self.assertIn("word_count", form.errors)


class SshTests(TestCase):
    def _client(self, exit_status, stderr=b""):
        client = MagicMock()
        stdout = MagicMock()
        stdout.read.return_value = b"ok"
        stdout.channel.recv_exit_status.return_value = exit_status
        err = MagicMock()
        err.read.return_value = stderr
        client.exec_command.return_value = (MagicMock(), stdout, err)
        return client

    def test_warning_on_stderr_is_not_failure(self):
        with patch("user_devices.commands.paramiko.SSHClient", return_value=self._client(0, b"warning")):
            self.assertEqual(execute_ssh_command("h", "u", "p", "cmd"), (True, "ok"))

    def test_nonzero_exit_is_failure(self):
        with patch("user_devices.commands.paramiko.SSHClient", return_value=self._client(1, b"boom")):
            self.assertEqual(execute_ssh_command("h", "u", "p", "cmd"), (False, "boom"))

    def test_gpio_mode_failure_stops_before_write(self):
        gw = MagicMock(ip_address="h", ssh_username="u", ssh_password="p")
        with patch("user_devices.commands.execute_ssh_command", return_value=(False, "nope")) as ssh, \
             patch("user_devices.commands.time.sleep"):
            success, _ = set_pin_status(gw, 5, "on")
        self.assertFalse(success)
        self.assertEqual(ssh.call_count, 1)


class ManagementCommandTests(TestCase):
    def test_midnight_aggregation_requires_confirm(self):
        with patch("user_devices.management.commands.test_midnight_aggregation.midnight_energy_aggregation") as task:
            call_command("test_midnight_aggregation", stdout=StringIO())
            task.assert_not_called()
            call_command("test_midnight_aggregation", "--confirm", stdout=StringIO())
            task.assert_called_once()

    def test_fake_energy_data_keeps_timestamps_and_is_idempotent(self):
        gw = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        meter = Device.objects.create(name="Energy Meter 1", Gateway=gw)
        call_command("generate_fake_data", "--days", "1", stdout=StringIO())
        count = EnergyData.objects.filter(device_name=meter).count()
        self.assertEqual(count, 96)
        # i timestamp sono quelli generati (distribuiti sulla giornata), non "adesso"
        first = EnergyData.objects.filter(device_name=meter).order_by("timestamp").first()
        self.assertLess(first.timestamp, timezone.now() - timedelta(hours=1))
        call_command("generate_fake_data", "--days", "1", stdout=StringIO())
        self.assertEqual(EnergyData.objects.filter(device_name=meter).count(), count)


class RevealOnceTests(TestCase):
    def test_reveal_once(self):
        gw = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        cred = GatewayMqttCredentials.objects.get(gateway=gw)
        password = cred.password_plaintext
        self.assertTrue(password)
        self.assertEqual(cred.reveal_once(), password)
        stale_copy = GatewayMqttCredentials.objects.get(pk=cred.pk)
        self.assertEqual(stale_copy.reveal_once(), "")
