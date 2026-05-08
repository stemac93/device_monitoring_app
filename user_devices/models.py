from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError


# Modalità di trasporto/lettura dati per un Gateway. Il task Celery
# `scan_and_read_devices` instrada in base a questo campo: cache MQTT
# popolata da Telegraf, polling Modbus TCP diretto via mbusd in VPN
# (vecchio comportamento, utile per debug), oppure polling DLMS.
GATEWAY_PROTOCOL_MODES = (
    ("mqtt", "MQTT (gateway pubblica via Telegraf)"),
    ("modbus_direct", "Modbus TCP diretto (server fa polling via mbusd in VPN)"),
    ("dlms", "DLMS (smart meter, polling diretto)"),
)


# Il device ha anche un ip associato
class Gateway(models.Model):
    user = models.ManyToManyField(User, related_name='user_gateway')
    name = models.CharField(max_length=50, default='not assigned') 
    ssh_username = models.CharField(max_length=50, default='ssh_user')  # SSH username
    ssh_password = models.CharField(max_length=100, default='ssh_psw')  # SSH password
    ip_address = models.CharField(max_length=50)
    performance_factor = models.FloatField(default=0, help_text="Performance factor of the plant")
    protocol_mode = models.CharField(
        max_length=20,
        choices=GATEWAY_PROTOCOL_MODES,
        default="mqtt",
        help_text=(
            "MQTT: il gateway pubblica via Telegraf, server consuma. "
            "Modbus TCP diretto: server fa polling Modbus TCP via mbusd in VPN "
            "(modalità legacy, utile per debug e per gateway non ancora migrati). "
            "DLMS: smart meter via DLMS/COSEM."
        ),
    )

    class Meta:
        verbose_name = "Gateway"
        verbose_name_plural = "Gateways"
        ordering = ['name']

    def __str__(self):
        return f"Name: {self.name}, Ip address: {self.ip_address}"

class Device(models.Model):
    user = models.ManyToManyField(User, related_name='user_device')
    Gateway = models.ForeignKey(Gateway, null=True, on_delete=models.CASCADE, related_name='devices')
    name = models.CharField(max_length=100, unique=True)
    is_enabled = models.BooleanField(default=False, help_text="Enable/Disable monitoring for this device")
    slave_id = models.IntegerField(default=-1, help_text="Slave ID of the device(nr between 1 to 247)", null=True, blank=True)
    start_address = models.CharField(default = 0, help_text="Starting Modbus address in hexadecimal (e.g., 0x0280)", null=True, blank=True)
    word_count = models.PositiveIntegerField(default=1, help_text="Total number of consecutive words to read", null=True, blank=True)
    port = models.IntegerField(default=0)
    availability = models.FloatField(default=0, help_text="Availability of the device")
    show_energy = models.BooleanField(default=False, help_text="Show real time energy production/consumption")
    show_energy_daily = models.BooleanField(default=False, help_text="Show daily energy production/consumption")
    show_energy_weekly = models.BooleanField(default=False, help_text="Show weekly energy production/consumption")
    show_energy_monthly = models.BooleanField(default=False, help_text="Show monthly energy production/consumption")
    daily_production = models.FloatField(default=0, help_text="Daily production of the device")
    daily_consumption = models.FloatField(default=0, help_text="Daily consumption of the device")
    register_type = models.CharField(
        max_length=10,
        choices=[('input', 'Input Register'), ('holding', 'Holding Register')],
        default='input',
        help_text='Type of Modbus register to read (Input or Holding)',
        null=True,
        blank=True
    )
    protocol = models.CharField(
        max_length=10,
        choices=[('modbus', 'MODBUS'), ('dlms', 'DLMS')],
        default='modbus',
        help_text='Type of protocol for the readings'
    )
    
    class Meta:
        verbose_name = "Device"
        verbose_name_plural = "Devices"
        ordering = ['name']  
    def __str__(self):
        return f"{self.name}"

class DeviceVariable(models.Model):   
    VARIABLE_TYPE_CHOICES = [
        ('memory', 'Memory Mapping'),
        ('computed', 'Computed Variable'),
    ]
    variable_type = models.CharField(
        max_length=10,
        choices=VARIABLE_TYPE_CHOICES,
        default='memory',
        help_text="Choose the type of variable to configure (Memory Mapping or Computed Variable)"
    )
    var_name = models.CharField(max_length=100, help_text="Name of the variable (e.g., Voltage, Power)", null=True, blank=True)
    unit = models.CharField(max_length=20, help_text="Measurement unit (e.g., V, A, W)", null=True, blank=True)
    show_on_graph = models.BooleanField(default=False, help_text="Show this variable on the graph")
    show_in_homepage = models.BooleanField(default=False, help_text="Show this variable in the homepage")
    order = models.PositiveIntegerField(default=0) 

    class Meta:
        ordering = ['order']  # Ensure sorted display
        abstract = True

    def save(self, *args, **kwargs):
        # If this variable is selected as X-axis, deselect others as X-axis for the same device
        if self.show_on_graph and hasattr(self, 'device') and self.device:
            ComputedVariable.objects.filter(device=self.device, show_on_graph=True).exclude(pk=self.pk).update(show_on_graph=False)
            ModbusMappingVariable.objects.filter(device=self.device, show_on_graph=True).exclude(pk=self.pk).update(show_on_graph=False)
            DlmsMappingVariable.objects.filter(device=self.device, show_on_graph=True).exclude(pk=self.pk).update(show_on_graph=False)
            
        super().save(*args, **kwargs)

class ModbusMappingVariable(DeviceVariable):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="modbus_variables", null=True, blank=True)
    address = models.CharField(default="",help_text="Address of the mapped value", null=True, blank=True)
    conversion_factor = models.CharField(default="1", help_text="Factor to convert raw data to physical value", null=True, blank=True)
    bit_length = models.PositiveIntegerField(
        choices=[(16, '16 bit'), (32, '32 bit'), (64, '64 bit')],
        default=16,
        help_text="Bit length of the register (16, 32, 64)"
    )
    is_signed = models.BooleanField(
        default=False,
        help_text="Interpret value as signed (True) or unsigned (False)"
    )
    endianness = models.CharField(
        max_length=10,
        choices=[('big', 'Big Endian'), ('little', 'Little Endian')],
        default='big',
        help_text="Endianness of the register (Big Endian or Little Endian)"
    )

    def __str__(self):
        return f"{self.var_name}"

class DlmsMappingVariable(DeviceVariable):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="dlms_variables", null=True, blank=True)
    conversion_factor = models.CharField(default="1", help_text="Factor to convert raw data to physical value", null=True, blank=True)
    obis_code = models.CharField(default="",help_text="Address of the mapped value", null=True, blank=True)
    column_idx = models.IntegerField(default=1, help_text="Column of the profile generic to read")

    def __str__(self):
        return f"{self.var_name}"
    
class ComputedVariable(DeviceVariable):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="computed_variables")
    formula = models.TextField(
        help_text="Formula to calculate the value (e.g., 'voltage * current'). Variables must match existing MemoryMapping variable names"
    )

    def __str__(self):
        return f"{self.var_name} (Computed)"

class GatewayData(models.Model):
    user = models.ManyToManyField(User, related_name='user_gateway_data')
    Gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name='gateway_data')
    data = models.JSONField()
    timestamp = models.DateTimeField()

    class Meta:
        verbose_name = "Gateway Data"
        verbose_name_plural = "Gateway Data"
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.Gateway} - {self.timestamp}"

class DeviceData(models.Model):
    user = models.ManyToManyField(User, related_name='user_device_data')
    Gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name='gateway_device_data')
    device_name = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='device_data')
    data = models.JSONField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Device Data"
        verbose_name_plural = "Device Data"
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.device_name} - {self.timestamp}"

class EnergyData(models.Model):
    user = models.ManyToManyField(User, related_name='user_energy_data')
    Gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name='gateway_energy_data')
    device_name = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='energy_data')
    data = models.JSONField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Energy Data"
        verbose_name_plural = "Energy Data"
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.device_name} - Energy - {self.timestamp}"

class Button(models.Model):
    Gateway = models.ForeignKey(Gateway, null=True, on_delete=models.CASCADE, related_name='buttons')
    label = models.CharField(max_length=100)  # Button name
    pin_number = models.IntegerField()  # GPIO pin number
    is_active = models.BooleanField(default=False)  # Current pin status
    show_in_user_page = models.BooleanField(default=False)  # Show this button to users

    class Meta:
        verbose_name = "Button"
        verbose_name_plural = "Buttons"
        ordering = ['label']

    def __str__(self):
        return f"{self.Gateway.name}, {self.Gateway.ip_address}"

class GatewayMqttCredentials(models.Model):
    """Credenziali MQTT per un Gateway.

    La password viene generata automaticamente alla creazione del Gateway
    (vedi signals.py). È visibile in chiaro UNA SOLA volta nell'admin, dopo
    di che viene azzerata e il flag `password_revealed` impedisce successive
    visualizzazioni.

    L'hash effettivo della password vive nel file passwd di Mosquitto, scritto
    dal helper container mosquitto-admin via API REST.
    """

    gateway = models.OneToOneField(
        Gateway,
        on_delete=models.CASCADE,
        related_name="mqtt_credentials",
    )
    username = models.CharField(max_length=64, unique=True)
    password_plaintext = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=(
            "Password in chiaro mostrata UNA volta dopo la creazione. "
            "Verrà azzerata dopo il primo accesso/visualizzazione."
        ),
    )
    password_revealed = models.BooleanField(
        default=False,
        help_text="True dopo che la password è stata vista almeno una volta.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Gateway MQTT credentials"
        verbose_name_plural = "Gateway MQTT credentials"

    def __str__(self):
        return f"{self.username} (gateway pk={self.gateway_id})"

    def reveal_once(self) -> str:
        """Ritorna la password in chiaro UNA SOLA VOLTA, poi la azzera."""
        if self.password_revealed:
            return ""
        pw = self.password_plaintext
        self.password_plaintext = ""
        self.password_revealed = True
        self.save(update_fields=["password_plaintext", "password_revealed", "updated_at"])
        return pw
