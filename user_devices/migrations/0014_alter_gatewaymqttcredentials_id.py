from django.db import migrations, models


class Migration(migrations.Migration):
    """0013 ha creato l'id come AutoField, ma DEFAULT_AUTO_FIELD e l'app config
    usano BigAutoField: senza questa migration makemigrations la rigenera."""

    dependencies = [
        ("user_devices", "0013_gatewaymqttcredentials"),
    ]

    operations = [
        migrations.AlterField(
            model_name="gatewaymqttcredentials",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
    ]
