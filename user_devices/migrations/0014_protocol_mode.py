from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("user_devices", "0013_gatewaymqttcredentials"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="gateway",
            name="use_mqtt",
        ),
        migrations.AddField(
            model_name="gateway",
            name="protocol_mode",
            field=models.CharField(
                choices=[
                    ("mqtt", "MQTT (gateway pubblica via Telegraf)"),
                    ("modbus_direct", "Modbus TCP diretto (server fa polling via mbusd in VPN)"),
                    ("dlms", "DLMS (smart meter, polling diretto)"),
                ],
                default="mqtt",
                max_length=20,
                help_text=(
                    "MQTT: il gateway pubblica via Telegraf, server consuma. "
                    "Modbus TCP diretto: server fa polling Modbus TCP via mbusd in VPN "
                    "(modalità legacy, utile per debug e per gateway non ancora migrati). "
                    "DLMS: smart meter via DLMS/COSEM."
                ),
            ),
        ),
    ]
