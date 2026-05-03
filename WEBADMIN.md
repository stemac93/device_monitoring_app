# WEBADMIN — gestione gateway dall'interfaccia web

Aggiunge alla repo la possibilità di **creare/cancellare gateway dall'admin
Django** con provisioning automatico delle credenziali MQTT.

## Cosa cambia rispetto al patch base

- Nuovo container **`mosquitto-admin`** (FastAPI) che gestisce il file
  `passwd` e l'`acl` di Mosquitto e fa SIGHUP al container del broker.
  Non esposto fuori dalla rete docker.
- Nuovo modello **`GatewayMqttCredentials`** che salva la password generata
  per ogni gateway (visibile UNA VOLTA in admin).
- Signal Django sul Gateway: alla creazione genera password random e
  configura Mosquitto via `mosquitto-admin`. Alla cancellazione pulisce.
- Admin Django: pulsante **"Scarica bundle gateway"** che esporta un
  `.tar.gz` con `telegraf.conf`, `ca.crt`, `telegraf.env` (con la password
  in chiaro) e `README.md`.
- Azione **"Rigenera credenziali MQTT"** sulla lista Gateway.

## Architettura

```
┌─── Django web ────┐   HTTP+token   ┌── mosquitto-admin ──┐  files+SIGHUP  ┌── mosquitto ──┐
│ post_save signal  │ ─────────────► │ POST /users         │ ────────────► │ passwd, acl   │
│ admin "bundle"    │                │ POST /acl           │                │               │
│ admin "rigenera"  │                │ POST /reload        │                │               │
└───────────────────┘                └─────────────────────┘                └───────────────┘
        │
        │ Django scrive solo nel proprio DB (GatewayMqttCredentials)
        │ Mai scrive direttamente nel filesystem di mosquitto.
        ▼
   PostgreSQL
```

## Sicurezza

- Password MQTT: `secrets.token_urlsafe(32)` (~43 caratteri stampabili).
- La password in chiaro è salvata nel DB **solo finché non viene visualizzata
  o usata per il bundle**. Dopo, il campo è azzerato e `password_revealed=True`.
- Il broker tiene solo l'hash PBKDF2 nel `passwd`.
- Il container `mosquitto-admin` accetta solo richieste con header
  `Authorization: Bearer <token>` dove `<token>` = `MOSQUITTO_ADMIN_TOKEN`
  generato dal `bootstrap.sh` (32 byte casuali esadecimali).
- `mosquitto-admin` NON è esposto fuori dalla rete docker (no `ports:`).
- Per fare SIGHUP, `mosquitto-admin` ha read-only del Docker socket.
  Se compromesso può solo lanciare `docker kill -s HUP` su container
  arbitrari (no exec, no run privilegiato), il blast radius è limitato.

## Procedura

```bash
# 1. Applica il patch
cd ~/device_monitoring_app
tar xzf ~/webadmin-patch.tar.gz -C .
git add -A
git commit -m "feat: gateway provisioning automatico via web admin"

# 2. Reset stato e bootstrap (se non l'hai già fatto)
./reset.sh
./bootstrap.sh
# il bootstrap chiede solo: POSTGRES_PASSWORD, MQTT_CONSUMER_PASSWORD, SERVER_VPN_IP
# e genera automaticamente MOSQUITTO_ADMIN_TOKEN nel .env

# 3. Avvio
docker compose up -d --build

# 4. Migrate (include la nuova 0013_gatewaymqttcredentials)
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py collectstatic --noinput

# 5. Verifica salute helper
docker compose exec web curl -s http://mosquitto-admin:8080/health
# → {"ok":true,"passwd_exists":true,"acl_exists":true,"mosquitto_container":"..."}
```

## Aggiungere un gateway dal web

1. Apri `http://<server>:8000/admin/`
2. Naviga in **User devices → Gateways → Add gateway**
3. Compila `name`, `ip_address`, e gli utenti che ci accedono
4. **Salva**: in background il signal Django genera password,
   chiama `mosquitto-admin`, aggiorna `passwd` e `acl`, fa SIGHUP
5. Ti ritrovi nella pagina di dettaglio del Gateway
6. Sezione "MQTT" → clicca **"⬇ Scarica bundle gateway"**
7. Il browser scarica `gateway-<pk>-<name>-bundle.tar.gz`
8. La password è ora marcata `revealed=True` e non sarà più scaricabile
9. Segui il `README.md` dentro il bundle per installare Telegraf
   sul gateway

Se devi ri-scaricare un bundle (perso, o sostituendo gateway):
- Lista Gateway → seleziona il gateway → azione **"Rigenera credenziali MQTT"**
- Si ricreano nuove password, l'utente Mosquitto viene aggiornato,
  il vecchio bundle non è più valido (autenticazione fallirà sul gateway)

## Cancellare un gateway

Cancellazione standard dall'admin → il signal `post_delete`:
- chiama `DELETE /users/gw-<pk>` → mosquitto-admin rimuove da passwd
- ricostruisce ACL senza quel gateway
- SIGHUP a mosquitto

## Troubleshooting

| Sintomo | Causa probabile | Fix |
|--|--|--|
| Bottone "Scarica bundle" non appare | `mqtt_credentials` non creata | Salva di nuovo il gateway, controlla logs `web` |
| `mosquitto-admin` 401 da Django | Token disallineato | Verifica `MOSQUITTO_ADMIN_TOKEN` uguale in tutti i service del compose |
| Mosquitto non vede le nuove password | SIGHUP fallito | `docker compose logs mosquitto-admin` cerca "docker kill failed" |
| Bundle dice "MQTT_PUBLIC_ENDPOINT non impostato" | Manca env nel `.env` | Aggiungi `MQTT_PUBLIC_ENDPOINT=ssl://10.8.0.1:8883`, restart `web` |
| Telegraf sul gateway: "bad username or password" | Password rivelata e ri-scaricata bundle vecchio | Rigenera credenziali e riscarica bundle |

## File aggiunti dal patch

```
mosquitto-admin/
├── Dockerfile
├── requirements.txt
└── app.py                                       # FastAPI helper
user_devices/
├── admin.py                                     # MODIFICATO: bottone bundle, azione rigenera
├── admin_mqtt.py                                # NUOVO: registrazione GatewayMqttCredentials + view bundle
├── models.py                                    # MODIFICATO: aggiunto GatewayMqttCredentials
├── signals.py                                   # MODIFICATO: aggiunti signal post_save/delete Gateway
├── urls.py                                      # MODIFICATO: aggiunta url /mqtt/gateway/<pk>/bundle/
├── migrations/
│   └── 0013_gatewaymqttcredentials.py          # NUOVO
└── mqtt/
    └── admin_client.py                          # NUOVO: client REST per mosquitto-admin
docker-compose.yml                               # MODIFICATO: nuovi service, nuove env
bootstrap.sh                                     # MODIFICATO: genera MOSQUITTO_ADMIN_TOKEN, MQTT_PUBLIC_ENDPOINT
```
