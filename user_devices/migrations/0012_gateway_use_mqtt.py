from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("user_devices", "0011_alter_gatewaydata_timestamp"),
    ]

    operations = [
        migrations.AddField(
            model_name="gateway",
            name="use_mqtt",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Se True, i device Modbus di questo gateway vengono letti "
                    "dalla cache MQTT (Telegraf pubblica su MQTT). Se False, "
                    "polling Modbus TCP diretto come prima della migrazione."
                ),
            ),
        ),
    ]
