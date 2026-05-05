# Migrazione MQTT — guida unica completa

Questa è la guida definitiva per applicare la migrazione MQTT al server
clonato. Sostituisce tutte le guide precedenti (`MIGRATION.md`,
`SERVER_INSTALL.md`, `INSTALL_FRESH.md`, `WEBADMIN.md`).

## Obiettivo finale

```
┌─── GATEWAY (RPi in VPN) ───┐         ┌─── SERVER ───────────────────────┐
│                             │         │                                   │
│  mbusd 503 / 504           │         │  ┌─────────────┐                 │
│       ▲                    │  MQTTS  │  │ mosquitto   │ ports 8883      │
│       │                    │ ◄─────► │  └─────────────┘                 │
│   Telegraf                 │         │       ▲ SIGHUP                   │
│                            │         │       │                          │
└─────────────────────────────┘         │  ┌────┴───────────┐  POST API   │
                                        │  │ mosquitto-admin │ ◄─┐         │
                                        │  └─────────────────┘  │         │
                                        │       │  passwd, acl  │         │
                                        │       ▼               │         │
                                        │  /mosquitto/config/   │         │
                                        │                       │         │
                                        │  ┌────────────────────┴──┐      │
                                        │  │ Django (web/admin)    │      │
                                        │  │ + Celery + signal     │      │
                                        │  └───────────────────────┘      │
                                        │       │                          │
                                        │  ┌────┴────┐    ┌────────┐       │
                                        │  │ redis   │    │postgres│       │
                                        │  └─────────┘    └────────┘       │
                                        │                                   │
                                        │  ┌──────────────────────────┐    │
                                        │  │ mqtt_consumer            │    │
                                        │  │ (sottoscrive plants/#,   │    │
                                        │  │  popola cache redis)     │    │
                                        │  └──────────────────────────┘    │
                                        └───────────────────────────────────┘
```

8 service Docker. Niente di nativo sull'host (oltre Docker stesso).

## Caratteristiche chiave

- **Aggiunta gateway dall'admin web**: alla creazione, Django genera password,
  configura Mosquitto, espone bottone download bundle Telegraf
- **Bundle .tar.gz** scaricabile dall'admin con `telegraf.conf`, `ca.crt`,
  `telegraf.env` (precompilato), `README.md` con i comandi per il gateway
- **Password mostrata UNA volta**: dopo il download del bundle viene azzerata
  nel DB. Per ri-scaricare → azione "Rigenera credenziali"
- **TLS self-signed con SAN su IP VPN**: nessun dominio richiesto
- **`mosquitto-admin` helper container** isolato, non esposto fuori dalla
  rete docker, autenticato via token Bearer
- **DLMS invariato**: il polling DLMS continua come prima della migrazione

## Prerequisiti sul server

```bash
docker version              # >= 24.x
docker compose version      # v2.x (con spazio, NON docker-compose)
git --version
```

L'utente deve essere nel gruppo `docker`:
```bash
sudo usermod -aG docker $USER
newgrp docker
docker ps   # deve funzionare senza sudo
```

Servizi nativi che potrebbero conflittare con i container — disabilitali:
```bash
sudo systemctl stop redis-server postgresql 2>/dev/null
sudo systemctl disable redis-server postgresql 2>/dev/null
sudo ss -tulpn | grep -E ':(5432|6379|1883|8000|8883)\s'
# Non deve mostrare nulla, oppure solo docker-proxy
```

## Procedura

### 1. Clone del progetto

```bash
cd ~
git clone https://github.com/stemac93/device_monitoring_app.git energy_monitoring_mqtt
cd energy_monitoring_mqtt
git checkout -b feature/mqtt
```

### 2. Applica il patch

Hai ricevuto **`mqtt-final-patch.tar.gz`**. Estrailo nella root del progetto:

```bash
tar xzf ~/mqtt-final-patch.tar.gz -C .
chmod +x bootstrap.sh reset.sh gateway_setup/install_telegraf.sh
git status   # vedi cosa è cambiato/aggiunto
git add -A
git commit -m "feat: MQTT migration with web admin gateway provisioning"
```

### 3. Bootstrap interattivo

```bash
./bootstrap.sh
```

Ti chiede:
- Password PostgreSQL → scegli o usa  $(openssl rand -base64 24)
- Password MQTT consumer → idem
- IP server visto dai gateway via VPN → es. `10.0.100.1`

Genera automaticamente:
- `.env` (con tutte le variabili tra cui `MOSQUITTO_ADMIN_TOKEN` casuale)
- `mosquitto/config/passwd` (con utente `django-consumer`)
- `mosquitto/config/acl` (consumer-only, gateway aggiunti dall'admin web)
- `mosquitto/certs/{ca,server}.{key,crt}` (self-signed con SAN sull'IP VPN)
- `.gitignore` aggiornato

### 4. Build e avvio

```bash
docker compose up -d --build
```

La prima volta impiega ~1-2 minuti (mosquitto-admin scarica il pacchetto
docker.io di Debian per ottenere il CLI Docker).

### 5. Verifica

```bash
docker compose ps
```

**Devono esserci 8 service tutti `Up`**:
- `web`, `database`, `celery`, `celery-beat`, `redis`
- `mosquitto`, `mosquitto-admin`, `mqtt_consumer`

Test salute helper:
```bash
docker compose exec mqtt_consumer python -c \
  "import requests; r=requests.get('http://mosquitto-admin:8080/health'); print(r.status_code, r.text)"
# Output atteso: 200 {"ok":true,"passwd_exists":true,"acl_exists":true,...}
```

Test broker:
```bash
docker compose logs --tail=20 mqtt_consumer | grep "Connected to MQTT"
# Deve esserci una riga: Connected to MQTT broker
```

### 6. Migrazione DB Django + superuser

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py collectstatic --noinput
```

### 7. Verifica admin web

Apri `http://<ip-server>:8000/admin/`, login con il superuser.

### 8. Aggiungi il primo gateway

1. **User devices → Gateways → Add gateway**
2. Compila campi (`name`, `ip_address`, `user`)
3. Lascia `Use mqtt` come vuoi (questo flag è per la migrazione live, in
   un setup fresh può restare a default = false)
4. **Salva**

Subito dopo il salvataggio:
- Signal Django genera password random
- Chiama `mosquitto-admin` → aggiunge `gw-<pk>` al passwd
- Riscrive l'ACL con tutti i gateway
- Manda SIGHUP a Mosquitto

5. Sei nella pagina di dettaglio del gateway. Sezione **MQTT** mostra
   un bottone verde **⬇ Scarica bundle gateway**
6. Click → scarichi `gateway-<pk>-<name>-bundle.tar.gz`

**ATTENZIONE**: la password è ora consumata. Per riscaricare il bundle
devi rigenerare le credenziali (vedi "Rigenera credenziali" sotto).

### 9. Configura Telegraf sul gateway

Apri il bundle e segui il `README.md` interno. In sintesi:

```bash
# Sul tuo PC, scompatta il bundle
tar xzf gateway-1-MioGateway-bundle.tar.gz -C ~/gw1-config

# Copia sul gateway via SSH
GATEWAY=root@10.0.100.42
scp ~/gw1-config/telegraf.conf $GATEWAY:/etc/telegraf/telegraf.conf
scp ~/gw1-config/ca.crt        $GATEWAY:/etc/telegraf/ca.crt
scp ~/gw1-config/telegraf.env  $GATEWAY:/etc/default/telegraf
scp gateway_setup/install_telegraf.sh $GATEWAY:/tmp/

# Sul gateway
ssh $GATEWAY
sudo bash /tmp/install_telegraf.sh
sudo chown telegraf:telegraf /etc/telegraf/ca.crt /etc/telegraf/telegraf.conf
sudo chmod 600 /etc/default/telegraf
sudo systemctl restart telegraf
sudo journalctl -u telegraf -n 50
```

### 10. Verifica end-to-end

Sul server:
```bash
# Vedi messaggi MQTT in arrivo
docker compose exec mosquitto mosquitto_sub \
  -h localhost -p 1883 \
  -u django-consumer -P "$(grep MQTT_CONSUMER_PASSWORD .env | cut -d= -f2)" \
  -t 'plants/+/devices/+/raw' -v

# Cache Redis si popola
docker compose exec redis redis-cli KEYS 'mqtt:rawdata:*'

# Celery processa
docker compose logs --tail=30 celery | grep "MQTT cache hit"

# Dati nel DB
docker compose exec database psql -U $(grep POSTGRES_USER .env | cut -d= -f2) \
  -d $(grep POSTGRES_DB .env | cut -d= -f2) -c \
  "SELECT COUNT(*), MAX(timestamp) FROM user_devices_devicedata WHERE timestamp > NOW() - INTERVAL '5 minutes';"
```

## Se qualcosa va storto: reset completo

`reset.sh` cancella container, volumi e file di configurazione generati.
Lo usi solo su questo server di test, mai in produzione:

```bash
./reset.sh
# digita RESET per confermare

# Poi ripartenza
./bootstrap.sh
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

## Operazioni comuni

### Rigenera credenziali per un gateway

Se hai perso il bundle o devi ruotare la password:

1. Admin → Gateways → seleziona la riga del gateway
2. Dropdown azioni in alto → **Rigenera credenziali MQTT** → Vai
3. Apri il dettaglio del gateway → bottone **⬇ Scarica bundle gateway**
4. Il vecchio bundle non funziona più, sostituiscilo sul gateway

### Cancella un gateway

Cancellazione standard dall'admin → signal `post_delete` rimuove l'utente
da Mosquitto e ricostruisce l'ACL automaticamente.

### Vedi stato MQTT in tempo reale

```bash
# Tutti i topic
docker compose exec mosquitto mosquitto_sub \
  -h localhost -p 1883 \
  -u django-consumer -P "$(grep MQTT_CONSUMER_PASSWORD .env | cut -d= -f2)" \
  -t '#' -v

# Solo dati grezzi
docker compose exec mosquitto mosquitto_sub \
  -h localhost -p 1883 \
  -u django-consumer -P "$(grep MQTT_CONSUMER_PASSWORD .env | cut -d= -f2)" \
  -t 'plants/+/devices/+/raw' -v
```

### Logs utili

```bash
docker compose logs -f mqtt_consumer        # arrivo dati
docker compose logs -f mosquitto-admin      # provisioning credenziali
docker compose logs -f mosquitto            # broker
docker compose logs -f celery               # processing
docker compose logs -f web                  # django
```

## Architettura sicurezza

- `MOSQUITTO_ADMIN_TOKEN` (32 byte hex) condiviso tra `web`/`celery` e
  `mosquitto-admin`. Senza token l'helper risponde 401
- `mosquitto-admin` non ha `ports:` esposti, raggiungibile solo dalla rete
  docker interna
- Helper ha bind read-only del Docker socket per fare SIGHUP
- Password MQTT generate con `secrets.token_urlsafe(32)`
- DB conserva password in chiaro **solo finché non viene mostrata/usata**
- Mosquitto memorizza solo PBKDF2 del passwd
- File `.env`, `mosquitto/config/passwd`, `mosquitto/certs/*.key|crt` in
  `.gitignore` automatico

## File chiave del patch

```
mosquitto-admin/                  # helper FastAPI
├── Dockerfile
├── app.py
└── requirements.txt

user_devices/
├── admin.py                      # bottone bundle, azione rigenera
├── admin_mqtt.py                 # registrazione admin + view bundle
├── models.py                     # +Gateway.use_mqtt, +GatewayMqttCredentials
├── signals.py                    # post_save Gateway → MQTT provision
├── tasks.py                      # Modbus polling → cache MQTT (DLMS invariato)
├── urls.py                       # +/mqtt/gateway/<pk>/bundle/
├── migrations/
│   ├── 0012_gateway_use_mqtt.py
│   └── 0013_gatewaymqttcredentials.py
├── mqtt/
│   ├── admin_client.py           # client REST per mosquitto-admin
│   ├── cache.py                  # Redis cache RAW
│   └── consumer.py               # consumer MQTT long-running
└── management/commands/
    └── export_telegraf_config.py # genera telegraf.conf

mosquitto/
├── config/
│   ├── mosquitto.conf            # config broker
│   ├── passwd                    # generato da bootstrap (gitignored)
│   └── acl                       # generato da bootstrap (gitignored)
└── certs/                        # generati da bootstrap (gitignored)

energy_monitoring/
└── celery.py                     # schedule MQTT-aware

gateway_setup/
├── install_telegraf.sh           # script setup gateway
└── telegraf.env.example          # template env

bootstrap.sh                      # configurazione iniziale interattiva
reset.sh                          # ripristino fresh
docker-compose.yml                # 8 service
requirements.txt                  # +paho-mqtt
MIGRATION.md, WEBADMIN.md         # documentazione storica (riferimento)
```
