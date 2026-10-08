from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from user_devices.models import Button, Device, Gateway, ModbusMappingVariable


class ToggleButtonViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="pw")
        self.gateway = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        self.gateway.user.add(self.user)
        self.button = Button.objects.create(Gateway=self.gateway, label="Relay", pin_number=5, show_in_user_page=True)
        self.hidden = Button.objects.create(Gateway=self.gateway, label="Maint", pin_number=6, show_in_user_page=False)
        self.url = reverse("toggle_button", args=[self.button.id])

    def test_get_not_allowed(self):
        self.client.login(username="u", password="pw")
        with patch("user_devices.views.set_pin_status") as pin:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        pin.assert_not_called()

    def test_anonymous_redirected_to_login(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_hidden_button_not_toggleable(self):
        self.client.login(username="u", password="pw")
        with patch("user_devices.views.set_pin_status") as pin:
            response = self.client.post(reverse("toggle_button", args=[self.hidden.id]))
        self.assertEqual(response.status_code, 404)
        pin.assert_not_called()

    def test_toggle_and_no_open_redirect(self):
        self.client.login(username="u", password="pw")
        with patch("user_devices.views.set_pin_status", return_value=(True, "")):
            response = self.client.post(self.url, HTTP_REFERER="https://evil.example/")
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.button.refresh_from_db()
        self.assertTrue(self.button.is_active)


class AdminToggleButtonTests(TestCase):
    def setUp(self):
        User.objects.create_superuser(username="admin", password="pw", email="a@example.com")
        gateway = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        self.button = Button.objects.create(Gateway=gateway, label="Relay", pin_number=5)
        self.url = reverse("admin:toggle_button_action", args=[self.button.pk])
        self.client.login(username="admin", password="pw")

    def test_admin_toggle_rejects_get(self):
        with patch("user_devices.admin.set_pin_status") as pin:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        pin.assert_not_called()

    def test_admin_toggle_post(self):
        with patch("user_devices.admin.set_pin_status", return_value=(True, "")):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.button.refresh_from_db()
        self.assertTrue(self.button.is_active)

    def test_changelist_renders_post_button(self):
        response = self.client.get(reverse("admin:user_devices_button_changelist"))
        self.assertContains(response, f'formaction="{self.url}" formmethod="post"')


class HomeViewTests(TestCase):
    def test_home_with_device_without_data(self):
        """Una variabile in homepage su un device senza DeviceData non deve dare 500."""
        user = User.objects.create_user(username="u", password="pw")
        gateway = Gateway.objects.create(name="gw", ip_address="10.0.0.1")
        gateway.user.add(user)
        device = Device.objects.create(name="inv", Gateway=gateway, is_enabled=True)
        ModbusMappingVariable.objects.create(device=device, var_name="P", address="0x0000", show_in_homepage=True)

        self.client.login(username="u", password="pw")
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
