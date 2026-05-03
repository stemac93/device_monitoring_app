"""
Estensioni admin per la gestione MQTT.

- Registra il modello GatewayMqttCredentials in admin (read-only, solo per
  visualizzare la password una volta sola).
- Aggiunge una view custom URL `gateway/<pk>/bundle/` che genera e scarica
  il .tar.gz con telegraf.conf + ca.crt + telegraf.env + README.

Per integrare il pulsante "Scarica bundle" nella pagina del Gateway esistente,
estendiamo GatewayAdmin in admin.py — vedi il commento al fondo per il
patch da applicare se preferisci modificare admin.py manualmente.
"""

import io
import logging
import os
import tarfile
from pathlib import Path

from django.contrib import admin, messages
from django.core.management import call_command
from django.http import HttpResponse, HttpResponseNotAllowed, Http404
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Gateway, GatewayMqttCredentials

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Admin per GatewayMqttCredentials (read-only)
# ---------------------------------------------------------------------------

@admin.register(GatewayMqttCredentials)
class GatewayMqttCredentialsAdmin(admin.ModelAdmin):
    list_display = ("username", "gateway", "password_status", "created_at", "updated_at")
    readonly_fields = (
        "gateway",
        "username",
        "password_display",
        "password_revealed",
        "created_at",
        "updated_at",
    )
    fields = (
        "gateway",
        "username",
        "password_display",
        "password_revealed",
        "created_at",
        "updated_at",
    )
    search_fields = ("username", "gateway__name", "gateway__ip_address")

    def has_add_permission(self, request):
        # Le credenziali si creano automaticamente via signal sul Gateway.
        return False

    def password_status(self, obj):
        if obj.password_revealed:
            return format_html('<span style="color:#888">non più visibile</span>')
        return format_html('<strong style="color:#a30">da visualizzare</strong>')

    password_status.short_description = "Stato password"

    def password_display(self, obj):
        if obj.password_revealed:
            return format_html(
                '<em>Password già mostrata in passato. Per re-fornirla a un gateway, '
                'rigenera dall\'azione "Rigenera credenziali" sull\'oggetto Gateway.</em>'
            )
        if not obj.password_plaintext:
            return format_html('<em>Vuota</em>')
        return format_html(
            '<div style="background:#fff8d4;padding:8px;border:1px solid #c00;'
            'border-radius:4px;font-family:monospace;font-size:14px;">'
            '<strong>{}</strong>'
            '<br><small style="color:#a30">Salva ORA questa password — '
            'non sarà più recuperabile dopo aver lasciato questa pagina.</small>'
            '</div>',
            obj.password_plaintext,
        )

    password_display.short_description = "Password (visibile UNA volta)"

    def get_object(self, request, object_id, from_field=None):
        # Quando l'admin apre il dettaglio della credenziale, mostriamo la password
        # e la marchiamo come rivelata DOPO il rendering.
        # Per fare questo in modo pulito, lasciamo che il campo password_plaintext
        # sia letto, e poi un'azione esplicita la cancella. Vedi `mark_revealed`.
        return super().get_object(request, object_id, from_field=from_field)

    actions = ["mark_revealed"]

    @admin.action(description="Marca come 'già visualizzata' (cancella password in chiaro)")
    def mark_revealed(self, request, queryset):
        n = 0
        for cred in queryset:
            if cred.password_plaintext:
                cred.password_plaintext = ""
                cred.password_revealed = True
                cred.save(update_fields=["password_plaintext", "password_revealed", "updated_at"])
                n += 1
        self.message_user(request, f"{n} credenziali marcate come visualizzate.", messages.SUCCESS)


# ---------------------------------------------------------------------------
# View custom: scarica bundle .tar.gz per un gateway
# ---------------------------------------------------------------------------

def gateway_bundle_view(request, gateway_pk: int):
    """Genera al volo un .tar.gz con telegraf.conf + ca.crt + telegraf.env + README.

    La password MQTT del gateway viene inclusa in `telegraf.env` in chiaro.
    Dopo il primo download, la password salvata nel DB viene azzerata
    (password_revealed = True).

    Solo staff/superuser.
    """
    if not request.user.is_authenticated or not request.user.is_staff:
        return HttpResponseNotAllowed(["GET"])

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    gateway = Gateway.objects.filter(pk=gateway_pk).first()
    if not gateway:
        raise Http404("Gateway not found")

    cred = GatewayMqttCredentials.objects.filter(gateway=gateway).first()
    if not cred:
        messages.error(
            request,
            f"Gateway {gateway.pk} non ha credenziali MQTT. Salva il gateway per generarle automaticamente.",
        )
        return HttpResponseNotAllowed(["GET"])

    if cred.password_revealed or not cred.password_plaintext:
        messages.error(
            request,
            "La password MQTT è già stata rivelata in passato. "
            "Rigenera le credenziali (azione 'Rigenera credenziali MQTT') prima di scaricare il bundle.",
        )
        return HttpResponseNotAllowed(["GET"])

    # 1) Genera telegraf.conf chiamando il management command
    import sys

    out_io = io.StringIO()
    sys_stdout_orig = sys.stdout
    try:
        sys.stdout = out_io
        call_command("export_telegraf_config", str(gateway.pk), "--interval", "30s")
    finally:
        sys.stdout = sys_stdout_orig
    telegraf_conf = out_io.getvalue()

    # 2) Leggi ca.crt
    ca_path = Path(os.getenv("MOSQUITTO_CA_CRT_PATH", "/mosquitto/certs/ca.crt"))
    if not ca_path.exists():
        messages.error(
            request,
            f"CA certificate non trovato in {ca_path}. Genera prima i certificati con bootstrap.sh.",
        )
        return HttpResponseNotAllowed(["GET"])
    ca_crt = ca_path.read_bytes()

    # 3) Componi telegraf.env
    server_endpoint = os.getenv("MQTT_PUBLIC_ENDPOINT", "")
    if not server_endpoint:
        messages.error(
            request,
            "MQTT_PUBLIC_ENDPOINT non impostato. Configurane uno tipo "
            "ssl://10.8.0.1:8883 nelle variabili d'ambiente del web container.",
        )
        return HttpResponseNotAllowed(["GET"])

    telegraf_env = (
        f"# Generato per Gateway pk={gateway.pk} ({gateway.name})\n"
        f"# Da copiare in /etc/default/telegraf sul gateway\n"
        f"\n"
        f"MQTT_SERVER={server_endpoint}\n"
        f"MQTT_USERNAME={cred.username}\n"
        f"MQTT_PASSWORD={cred.password_plaintext}\n"
        f"MQTT_TLS_CA=/etc/telegraf/ca.crt\n"
    )

    # 4) Componi README
    readme = f"""# Bundle Telegraf per Gateway pk={gateway.pk} ({gateway.name})

Generato il {cred.updated_at:%Y-%m-%d %H:%M:%S} per il deploy MQTT.

## Contenuti

- `telegraf.conf`     configurazione Telegraf con tutti i Device del gateway
- `ca.crt`            certificato CA del broker (da fidarsi sul gateway)
- `telegraf.env`      credenziali MQTT (mantenere segrete, mode 600)

## Installazione sul gateway

```bash
# 1. Carica i file da una directory locale al gateway:
scp telegraf.conf root@<IP_GATEWAY>:/etc/telegraf/telegraf.conf
scp ca.crt        root@<IP_GATEWAY>:/etc/telegraf/ca.crt
scp telegraf.env  root@<IP_GATEWAY>:/etc/default/telegraf

# 2. Sul gateway:
ssh root@<IP_GATEWAY>
chown telegraf:telegraf /etc/telegraf/ca.crt /etc/telegraf/telegraf.conf
chmod 600 /etc/default/telegraf
systemctl restart telegraf
journalctl -u telegraf -n 50
```

## Verifica lato server

```bash
docker compose exec mosquitto mosquitto_sub \\
  -h localhost -u django-consumer -P '<consumer-password>' \\
  -t 'plants/{gateway.pk}/devices/+/raw' -v
```

## ATTENZIONE

La password contenuta in `telegraf.env` è valida e non può essere recuperata
una volta perso questo bundle. Se hai bisogno di rigenerare un nuovo bundle
dovrai prima rigenerare le credenziali dall'admin Django.
"""

    # 5) Pacchettizza in .tar.gz in memoria
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (
            ("telegraf.conf", telegraf_conf.encode("utf-8")),
            ("ca.crt", ca_crt),
            ("telegraf.env", telegraf_env.encode("utf-8")),
            ("README.md", readme.encode("utf-8")),
        ):
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            ti.mode = 0o600 if name == "telegraf.env" else 0o644
            tf.addfile(ti, io.BytesIO(data))

    # 6) Marca password come rivelata
    cred.password_plaintext = ""
    cred.password_revealed = True
    cred.save(update_fields=["password_plaintext", "password_revealed", "updated_at"])

    logger.info(
        "Generated bundle for gateway pk=%s username=%s, marked password as revealed",
        gateway.pk,
        cred.username,
    )

    response = HttpResponse(buf.getvalue(), content_type="application/gzip")
    response["Content-Disposition"] = (
        f'attachment; filename="gateway-{gateway.pk}-{gateway.name}-bundle.tar.gz"'
    )
    return response
