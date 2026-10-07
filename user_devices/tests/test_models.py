from django.test import TestCase, Client
from django.urls import reverse
from user_devices.models import Device, Button, Gateway
from django.contrib.auth.models import User

class UserDevicesTestCase(TestCase):

    def setUp(self):
        # Create test user
        self.user = User.objects.create_user(username='testuser', password='testpass')

        # Create test device
        self.gateway = Gateway.objects.create(
            ssh_username="ssh_testuser",
            ssh_password="ssh_testpass",
            ip_address="192.168.1.100"
        )

        # Add user to M2M field after creation
        self.gateway.user.add(self.user)

        # Create test device
        self.device = Device.objects.create(
            name="Test_Device",
            Gateway=self.gateway,
            slave_id=1,
            port=502
        )

        # Add user to M2M field after creation
        self.device.user.add(self.user)

        # Create test button
        self.button = Button.objects.create(
            label="Test_Button",
            pin_number=5,
            is_active=False,
            Gateway=self.gateway,
            show_in_user_page=False
        )

    def test_device_creation(self):
        self.assertEqual(Device.objects.count(), 1)
        self.assertEqual(self.device.name, "Test_Device")

    def test_button_creation(self):
        self.assertEqual(Button.objects.count(), 1)
        self.assertEqual(self.button.label, "Test_Button")

    def test_device_detail_view(self):
        self.client.login(username='testuser', password='testpass')
        response = self.client.get(reverse('device_detail', args=[self.device.name]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test_Device")

    def test_button_toggle_in_db(self):
        self.button.is_active = not self.button.is_active
        self.button.save()
        self.assertEqual(self.button.is_active, True)

    def test_unauthorized_access(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 302) # Redirect to login page

    def test_authenticated_access(self):
        self.client.login(username='testuser', password='testpass')
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)