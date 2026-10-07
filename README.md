# Energy Monitoring — Django + MQTT

Webapp Django per il monitoraggio di impianti energetici (fotovoltaico: produzione,
consumo, disponibilità, performance, irraggiamento). I dispositivi Modbus/DLMS
sono collegati a gateway Raspberry Pi installati sugli impianti. Il server
raccoglie i dati, li converte in grandezze fisiche e li mostra in una dashboard
per gli utenti e in un'interfaccia admin.

> **Novità di questa versione.** Prima il server interrogava direttamente i
> gateway in **Modbus TCP**, con un polling via VPN verso `mbusd`. Adesso ogni
> gateway esegue **Telegraf**: legge i registri Modbus in locale e li
> **pubblica via MQTT (TLS)** su un broker Mosquitto ospitato sul server.
> Il vecchio polling Modbus è ancora disponibile e si sceglie **per singolo
> gateway** con il campo `protocol_mode`, così puoi migrare un impianto alla volta.
> I dispositivi DLMS continuano a essere interrogati direttamente come prima.

---

## Indice

1. [Funzionalità](#funzionalità)
2. [Architettura](#architettura)
3. [Cosa cambia rispetto alla versione solo Modbus](#cosa-cambia-rispetto-alla-versione-solo-modbus)
4. [Prerequisiti](#prerequisiti)
5. [Deploy del server](#deploy-del-server)
6. [Deploy del client (gateway)](#deploy-del-client-gateway)
7. [Migrare un gateway esistente da Modbus TCP a MQTT](#migrare-un-gateway-esistente-da-modbus-tcp-a-mqtt)
8. [Operazioni comuni](#operazioni-comuni)
9. [Troubleshooting](#troubleshooting)
10. [Sicurezza](#sicurezza)
11. [Sviluppo e test](#sviluppo-e-test)
12. [Struttura del repository](#struttura-del-repository)

---

## Funzionalità

- **Gestione dispositivi**: gateway, dispositivi Modbus/DLMS e variabili
  (registri Modbus, codici OBIS e variabili calcolate con formule) si
  configurano dall'admin Django.
- **Acquisizione dati**: i registri grezzi vengono letti, convertiti in
  grandezze fisiche (fattore di conversione, endianness, segno, numero di bit)
  e salvati come JSON su PostgreSQL.
- **Energia e metriche di impianto**: energia giornaliera, settimanale e mensile,
  disponibilità, performance e aggregazione di mezzanotte.
- **Dashboard utente** (`/home/`): ogni utente vede solo i gateway e i
  dispositivi che gli sono stati assegnati.
- **Controllo GPIO via SSH**: pulsanti configurabili che accendono e spengono
  i pin del gateway.
- **Provisioning MQTT dall'admin**: quando crei un gateway vengono generati in
  automatico utente e password MQTT e l'ACL del broker. Dall'admin puoi poi
  scaricare un **bundle** pronto per il gateway, con `telegraf.conf`, `ca.crt`,
  le credenziali e un README.

### UI

**Admin**
![admin](https://github.com/user-attachments/assets/9f350c70-35c3-475f-9d02-c38245cf81a0)

**Dashboard utente**
![home](https://github.com/user-attachments/assets/27c1e3d6-d774-414e-aed3-e4882f23cc59)

### Stack

| Livello           | Tecnologia                                       |
|-------------------|--------------------------------------------------|
| Backend           | Django 5.1 (Python 3.11)                         |
| Database          | PostgreSQL                                       |
| Task asincroni    | Celery + Celery Beat, Redis come broker          |
| Messaggistica     | Mosquitto 2 (MQTT v5, TLS), paho-mqtt            |
| Agent sul gateway | Telegraf (InfluxData) + `mbusd`                  |
| Deploy            | Docker Compose v2                                |

---

## Architettura

```
┌──────── GATEWAY (Raspberry Pi, in VPN) ────────┐          ┌──────────────────────── SERVER (Docker) ───────────────────────┐
│                                                 │          │                                                                │
│  Dispositivi Modbus RTU ──► mbusd :503/:504...  │          │   ┌────────────┐ 8883 TLS   ┌─────────────────┐ SIGHUP          │
│                                 ▲               │  MQTTS   │   │ mosquitto  │◄───────────│ mosquitto-admin │ (docker.sock)   │
│                                 │ Modbus TCP    │ ───────► │   │  broker    │            │ FastAPI, interno│                 │
│                                 │ 127.0.0.1     │  :8883   │   └─────┬──────┘            └────────▲────────┘                 │
│                           ┌─────┴─────┐         │          │         │ 1883 (rete docker)         │ REST + Bearer token      │
│                           │ Telegraf  │─────────┘          │   ┌─────▼──────────┐                 │                          │
│                           └───────────┘                    │   │ mqtt_consumer  │         ┌───────┴──────────────┐           │
│                                                 │          │   │ (plants/#)     │         │ web (Django + admin) │ :8000     │
│  API DLMS HTTP :<port> ◄────────────────────────┼──────────┼───┼────────────────┼─────────┤ celery / celery-beat │           │
│  SSH (GPIO)            ◄────────────────────────┼──────────┼───┤                │         └───────┬──────────────┘           │
│  mbusd (solo legacy)   ◄────────────────────────┼──────────┼───┤                │                 │                          │
└─────────────────────────────────────────────────┘          │   └─────┬──────────┘                 │                          │
                                                             │         ▼                            ▼                          │
                                                             │   ┌──────────┐                 ┌──────────┐                     │
                                                             │   │  redis   │◄────────────────│ postgres │                     │
                                                             │   │ (cache)  │   Celery legge  └──────────┘                     │
                                                             │   └──────────┘   la cache e scrive in DB                        │
                                                             └────────────────────────────────────────────────────────────────┘
```

### Flusso dati (percorso MQTT)

1. **Telegraf** sul gateway legge ogni 30 s (valore predefinito) i registri
   Modbus di ogni dispositivo tramite `mbusd` su `tcp://127.0.0.1:<port>`.
2. Per ogni dispositivo pubblica un messaggio JSON sul topic
   `plants/{gateway_pk}/devices/{device_pk}/raw`, con un campo per registro
   chiamato `ir_0xNNNN` (input register) o `hr_0xNNNN` (holding register),
   QoS 1 e flag retained. Un dispositivo può avere più blocchi di lettura.
3. **Mosquitto** autentica il gateway (utente `gw-{pk}`) e, tramite l'ACL,
   gli permette di pubblicare solo sotto `plants/{pk}/#`.
4. **mqtt_consumer** (processo paho sempre attivo) è iscritto a `plants/#` e
   salva l'ultimo snapshot di registri in Redis, chiave
   `mqtt:rawdata:{device_pk}` con TTL di 15 minuti. **Non scrive nel DB.**
5. **Celery Beat** lancia `check_all_devices` → un task
   `scan_and_read_devices` per ogni gateway, protetto da un lock Redis.
   Per i dispositivi Modbus di un gateway con `protocol_mode=mqtt`, il task legge
   la cache. Se lo snapshot ha più di 5 minuti (`RAW_FRESHNESS_SECONDS`) lo
   scarta e il dispositivo non viene aggiornato, così durante le
   disconnessioni non si salvano dati finti.
6. Da qui la pipeline è **identica a prima**: `map_variables` →
   `compute_variables` → `compute_energy` → salvataggio su PostgreSQL
   (`DeviceData`, `EnergyData`). `compute_plant_metrics` e
   `midnight_energy_aggregation` lavorano solo sul DB.

### Servizi Docker (8)

| Servizio          | Ruolo                                                                  | Porte host  |
|-------------------|------------------------------------------------------------------------|-------------|
| `web`             | Django (admin, dashboard, download del bundle gateway)                 | `8000`      |
| `database`        | PostgreSQL                                                             | `5432`      |
| `redis`           | Broker Celery + cache dei registri MQTT                                | `6379`      |
| `celery`          | Worker: legge i dati, li elabora e li salva                            | —           |
| `celery-beat`     | Scheduler dei task periodici                                           | —           |
| `mosquitto`       | Broker MQTT: `8883` TLS per i gateway, `1883` solo sulla rete docker   | `8883`      |
| `mosquitto-admin` | Helper FastAPI che modifica `passwd` e `acl` e invia SIGHUP al broker  | — (interno) |
| `mqtt_consumer`   | Consumer MQTT → cache Redis                                            | —           |

---

## Cosa cambia rispetto alla versione solo Modbus

| Aspetto                     | Prima (solo Modbus)                                 | Ora (MQTT)                                                                   |
|-----------------------------|-----------------------------------------------------|------------------------------------------------------------------------------|
| Chi apre la connessione     | Il server interroga `mbusd` sul gateway via VPN     | Il gateway (Telegraf) pubblica verso il server sulla porta 8883              |
| Protocollo sulla VPN        | Modbus TCP in chiaro                                | MQTT v5 su TLS, autenticato per gateway                                      |
| Lettura registri            | pymodbus nel worker Celery                          | Telegraf in locale sul gateway                                               |
| Tolleranza ai disservizi    | Una lettura fallita è un dato perso                 | Il broker conserva l'ultimo messaggio (retained) e Telegraf ha un buffer     |
| Configurazione del gateway  | Solo `mbusd`                                        | `mbusd` + Telegraf, configurato con il bundle scaricato dall'admin           |
| Scelta del percorso         | —                                                   | Campo `Gateway.protocol_mode`: `mqtt` (predefinito), `modbus_direct`, `dlms` |
| DLMS                        | Polling diretto                                     | **Invariato**: polling diretto                                               |
| GPIO / SSH                  | Via SSH                                             | **Invariato**                                                                |

> Il server deve quindi **raggiungere comunque i gateway via VPN** se usi
> DLMS, i pulsanti GPIO (SSH) o gateway ancora in modalità legacy. Con il
> percorso MQTT, per la parte Modbus basta che il gateway raggiunga il server
> sulla porta 8883.

---

## Prerequisiti

### Server

- Linux con **Docker ≥ 24** e **Docker Compose v2** (`docker compose`, con lo
  spazio: non il vecchio `docker-compose`).
- `git` e `openssl`.
- L'utente che fa il deploy deve essere nel gruppo `docker`:
  ```bash
  sudo usermod -aG docker $USER
  newgrp docker
  docker ps          # deve funzionare senza sudo
  ```
- Un **IP fisso del server raggiungibile dai gateway**, di solito l'IP del
  server nella VPN (es. `10.8.0.1`). Finisce nel SAN del certificato TLS:
  se cambia, devi rigenerare i certificati.
- Porte libere: `8000`, `8883`, `5432`, `6379`. Disattiva eventuali servizi
  nativi in conflitto:
  ```bash
  sudo systemctl disable --now redis-server postgresql 2>/dev/null
  sudo ss -tulpn | grep -E ':(5432|6379|1883|8000|8883)\s'   # vuoto, o solo docker-proxy
  ```

### Gateway

- Raspberry Pi OS / Debian / Ubuntu con accesso root (o sudo) e accesso a
  Internet per installare Telegraf dal repo InfluxData.
- `mbusd` già installato e in ascolto **in locale** sulle porte usate dai
  dispositivi (es. `503`, `504`). Telegraf si collega a
  `tcp://127.0.0.1:<Device.port>`.
- Connettività verso il server su **TCP 8883**, di solito tramite VPN.
- Orologio sincronizzato (NTP): il timestamp dei messaggi serve per la
  verifica di freschezza lato server.

---

## Deploy del server

### 1. Clona il progetto

```bash
cd ~
git clone <url-del-repository> energy_monitoring_mqtt
cd energy_monitoring_mqtt
git checkout feature/mqtt          # oppure il branch/tag da deployare
chmod +x bootstrap.sh reset.sh gateway_setup/install_telegraf.sh
```

### 2. Bootstrap (una sola volta)

```bash
./bootstrap.sh
```

Lo script chiede:

| Domanda                                    | Esempio / suggerimento                  |
|--------------------------------------------|-----------------------------------------|
| Password PostgreSQL                        | vedi il punto 3 qui sotto               |
| Password MQTT consumer (`django-consumer`) | `openssl rand -base64 24`               |
| IP del server visto dai gateway via VPN    | `10.8.0.1`                              |

E genera:

- `.env` (modo 600): `POSTGRES_*`, `MQTT_CONSUMER_*`,
  `MQTT_PUBLIC_ENDPOINT=ssl://<IP>:8883` e un `MOSQUITTO_ADMIN_TOKEN`
  casuale;
- `mosquitto/config/passwd`, con il solo utente `django-consumer`;
- `mosquitto/config/acl`, che al bootstrap contiene solo il consumer (i
  gateway vengono aggiunti dall'admin web);
- `mosquitto/certs/`: una CA self-signed (`ca.crt`, `ca.key`) e il
  certificato del broker (`server.crt`, `server.key`) con **SAN = IP VPN**.

Modalità non interattiva (per esempio in CI o su un'altra macchina):

```bash
POSTGRES_PASSWORD=... MQTT_CONSUMER_PASSWORD=... SERVER_VPN_IP=10.8.0.1 \
  ./bootstrap.sh --non-interactive
```

Lo script non sovrascrive i file che esistono già. `--force` li ricrea tutti,
**compresa la CA**: in quel caso il `ca.crt` già installato sui gateway non è
più valido e devi ridistribuirlo.

### 3. Credenziali del database

Le credenziali Postgres stanno solo in `.env` (`POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB`). `docker-compose.yml` le passa sia al
container `database`, che crea utente e DB al primo avvio, sia ai container
Django (`web`, `celery`, `celery-beat`, `mqtt_consumer`), che le leggono in
`settings.py`. Non serve allineare nulla a mano.

Se vuoi valori diversi da quelli predefiniti (`postgres` / `energy_monitoring`),
modifica `.env` **prima del primo avvio**: Postgres crea utente e DB solo
quando il volume `postgres_data` è vuoto.

### 4. Imposta `ALLOWED_HOSTS`

Anche `ALLOWED_HOSTS` è scritto in `settings.py`. Aggiungi l'IP o il nome con
cui aprirai l'admin (es. l'IP VPN del server):

```python
ALLOWED_HOSTS = ['10.8.0.1', 'localhost', '127.0.0.1']
```

### 5. Build e avvio

```bash
docker compose up -d --build
docker compose ps        # 8 servizi "Up", mosquitto NON in "Restarting"
```

La prima build richiede 1–2 minuti, perché `mosquitto-admin` installa la CLI
Docker per mandare il SIGHUP al broker.

### 6. Database, superuser, file statici

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py collectstatic --noinput
```

### 7. Verifica il server

```bash
# Helper mosquitto-admin raggiungibile e file presenti
docker compose exec mqtt_consumer python -c \
  "import requests; r=requests.get('http://mosquitto-admin:8080/health'); print(r.status_code, r.text)"
# atteso: 200 {"ok":true,"passwd_exists":true,"acl_exists":true,...}

# Consumer connesso al broker
docker compose logs --tail=20 mqtt_consumer | grep "Connected to MQTT"

# TLS esposto correttamente (dal server o da un host in VPN)
openssl s_client -connect <IP_VPN_SERVER>:8883 -CAfile mosquitto/certs/ca.crt </dev/null 2>/dev/null \
  | grep "Verify return code"
# atteso: Verify return code: 0 (ok)
```

Admin: `http://<ip-server>:8000/admin/` — Dashboard utente: `http://<ip-server>:8000/home/`

### 8. Crea il gateway e i dispositivi nell'admin

1. **User devices → Gateways → Add gateway**: `name`, `ip_address` (l'IP VPN
   del gateway), `user` (chi deve vedere i dati), **`Protocol mode`**
   (`MQTT` è il valore predefinito; `Modbus TCP diretto` per il vecchio polling).
2. **Salva.** Il signal `post_save`:
   - crea `GatewayMqttCredentials` (utente `gw-<pk>`, password casuale);
   - chiama `mosquitto-admin`, che aggiunge l'utente a `passwd`, riscrive
     l'ACL (`rw plants/<pk>/#`) e invia SIGHUP al broker.
3. Crea i **Device** Modbus del gateway (`Gateway`, `protocol=modbus`,
   `is_enabled`, `slave_id`, `port` di mbusd). Nel campo **Preset** scegli il
   modello di inverter: blocchi di lettura e variabili vengono creati da soli
   (vedi [Preset dei dispositivi](#preset-dei-dispositivi)). Con
   "— Nessuno (mappatura manuale) —" inserisci a mano i **blocchi di lettura**
   (tipo di registro, `start_address` in esadecimale, `word_count`; un device
   può averne più di uno) e le **variabili**.

> Il bundle contiene un `telegraf.conf` generato dai dispositivi **Modbus
> abilitati** presenti in quel momento. Crea i dispositivi **prima** di
> scaricarlo; per aggiornarlo dopo, vedi
> [Aggiornare `telegraf.conf`](#aggiornare-telegrafconf-dopo-modifiche-ai-dispositivi).

### 9. Scarica il bundle del gateway

Nella pagina di dettaglio del gateway, sezione **MQTT**, clicca
**⬇ Scarica bundle gateway**. Ottieni `gateway-<pk>-<nome>-bundle.tar.gz`:

| File            | Destinazione sul gateway | Contenuto                                                             |
|-----------------|--------------------------|-----------------------------------------------------------------------|
| `telegraf.conf` | `/etc/telegraf/`         | Input Modbus per ogni dispositivo + output MQTT                       |
| `ca.crt`        | `/etc/telegraf/`         | CA del broker                                                         |
| `telegraf.env`  | `/etc/default/telegraf`  | `MQTT_SERVER`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TLS_CA`        |
| `README.md`     | —                        | Promemoria dei comandi                                                |

> 🔐 **La password viene mostrata una sola volta.** Al primo download viene
> cancellata dal DB (`password_revealed=True`) e resta solo il suo hash nel
> `passwd` di Mosquitto. Conserva il bundle finché non hai finito
> l'installazione. Se lo perdi, usa l'azione
> [Rigenera credenziali MQTT](#rigenera-le-credenziali-di-un-gateway).

---

## Deploy del client (gateway)

Tutti i comandi qui sotto usano come esempio `GATEWAY=root@10.8.0.42`.

### 1. Verifica i prerequisiti sul gateway

```bash
ssh $GATEWAY
systemctl status mbusd*            # mbusd attivo
ss -tlnp | grep -E ':50[0-9]\s'    # porte mbusd in ascolto (es. 503, 504)
timedatectl                        # "System clock synchronized: yes"
nc -zv 10.8.0.1 8883               # server raggiungibile sulla porta MQTT
```

### 2. Copia i file sul gateway

Dal tuo PC, dove hai scaricato il bundle e clonato il repository:

```bash
mkdir -p ~/gw42 && tar xzf gateway-42-Impianto-bundle.tar.gz -C ~/gw42

scp gateway_setup/install_telegraf.sh $GATEWAY:/tmp/
scp ~/gw42/telegraf.conf ~/gw42/ca.crt ~/gw42/telegraf.env $GATEWAY:/tmp/
```

### 3. Installa Telegraf

```bash
ssh $GATEWAY
sudo bash /tmp/install_telegraf.sh
```

Lo script aggiunge il repository APT ufficiale InfluxData, installa `telegraf`,
crea `/etc/telegraf` e `/var/log/telegraf` e abilita il servizio systemd.

### 4. Installa la configurazione

Sempre sul gateway:

```bash
sudo install -o telegraf -g telegraf -m 644 /tmp/telegraf.conf /etc/telegraf/telegraf.conf
sudo install -o telegraf -g telegraf -m 644 /tmp/ca.crt        /etc/telegraf/ca.crt
sudo install -o root     -g root     -m 600 /tmp/telegraf.env  /etc/default/telegraf
rm /tmp/telegraf.env /tmp/telegraf.conf /tmp/ca.crt

sudo systemctl restart telegraf
```

Il servizio systemd di Telegraf carica `/etc/default/telegraf` come
`EnvironmentFile`. Telegraf sostituisce i placeholder `${MQTT_SERVER}`,
`${MQTT_USERNAME}`, `${MQTT_PASSWORD}` e `${MQTT_TLS_CA}` nel `telegraf.conf`,
quindi **i segreti non finiscono mai nel file di configurazione**.

### 5. Verifica sul gateway

```bash
systemctl status telegraf
sudo tail -f /var/log/telegraf/telegraf.log     # logfile definito in telegraf.conf
sudo journalctl -u telegraf -n 50               # errori di avvio / parsing config

# Test a secco: legge i registri Modbus e stampa le metriche senza pubblicarle
sudo bash -c 'set -a; . /etc/default/telegraf; telegraf --config /etc/telegraf/telegraf.conf --test --input-filter modbus'
```

Nell'output di `--test` devono comparire righe `modbus,device_id=…,gateway_id=…
ir_0x0280=…i,…` (o `hr_0x…` per gli holding register).

### 6. Verifica end-to-end dal server

```bash
# Messaggi in arrivo sul broker
docker compose exec mosquitto mosquitto_sub -h localhost -p 1883 \
  -u django-consumer -P "$(grep MQTT_CONSUMER_PASSWORD .env | cut -d= -f2)" \
  -t 'plants/42/devices/+/raw' -v

# Cache Redis popolata dal consumer
docker compose exec redis redis-cli KEYS 'mqtt:rawdata:*'

# Celery usa la cache (richiede protocol_mode=mqtt sul gateway)
docker compose logs --tail=50 celery | grep "MQTT cache hit"

# Dati salvati negli ultimi 5 minuti
docker compose exec database psql -U "$(grep POSTGRES_USER .env | cut -d= -f2)" \
  -d "$(grep POSTGRES_DB .env | cut -d= -f2)" -c \
  "SELECT COUNT(*), MAX(timestamp) FROM user_devices_devicedata WHERE timestamp > NOW() - INTERVAL '5 minutes';"
```

Infine apri la dashboard `/home/` con un utente associato al gateway.

---

## Migrare un gateway esistente da Modbus TCP a MQTT

I due percorsi convivono, quindi puoi migrare un gateway alla volta senza
fermare la raccolta dati.

1. **Gateway esistente senza credenziali MQTT.** Le credenziali vengono create
   solo quando **si crea** un gateway. Per i gateway che esistevano prima della
   migrazione: **Admin → Gateways → seleziona → azione "Rigenera credenziali
   MQTT"**. L'azione crea le credenziali, aggiorna il broker e abilita il
   download del bundle.
2. Imposta `protocol_mode = Modbus TCP diretto`: il server continua con il
   polling Modbus TCP. **Attenzione:** la migration `0014_protocol_mode` mette
   tutti i gateway esistenti su `MQTT`, quindi dopo l'aggiornamento riporta a
   `Modbus TCP diretto` quelli non ancora migrati.
3. Installa Telegraf sul gateway come in
   [Deploy del client](#deploy-del-client-gateway). `mbusd` accetta più
   client, quindi per un po' leggono sia Telegraf sia il server.
4. Verifica che i messaggi arrivino (`mosquitto_sub`, `KEYS mqtt:rawdata:*`).
5. Imposta **`protocol_mode = MQTT`** sul gateway e salva. Dal ciclo successivo di Celery
   Beat, i dispositivi Modbus di quel gateway vengono letti dalla cache
   (`MQTT cache hit` nei log di `celery`).
6. **Rollback**: rimetti `protocol_mode = Modbus TCP diretto` e si torna
   subito al polling diretto.

---

## Operazioni comuni

### Aggiornare `telegraf.conf` dopo modifiche ai dispositivi

Se aggiungi o togli un dispositivo, oppure cambi i suoi blocchi di lettura,
`slave_id` o `port`, devi rigenerare la
configurazione del gateway. **Non serve toccare le credenziali**: basta il
solo `telegraf.conf`.

```bash
# Sul server
docker compose exec web python manage.py export_telegraf_config 42 --interval 30s > telegraf-gw42.conf

# Copia sul gateway e riavvia Telegraf
scp telegraf-gw42.conf $GATEWAY:/tmp/telegraf.conf
ssh $GATEWAY 'sudo install -o telegraf -g telegraf -m 644 /tmp/telegraf.conf /etc/telegraf/telegraf.conf && sudo systemctl restart telegraf'
```

Le modifiche alle sole **variabili** (fattori di conversione, formule,
endianness…) **non** richiedono di aggiornare Telegraf, perché la conversione
avviene sul server.

### Preset dei dispositivi

I preset sono file JSON in `user_devices/presets/<categoria>/` (per ora solo
`inverter/`). Alla creazione di un device, il preset scelto crea:

- i **blocchi di lettura** (input e/o holding register, al massimo 125 word
  ciascuno);
- le **variabili Modbus** (indirizzo, tipo di registro, 16/32/64 bit, segno,
  ordine delle word, fattore di conversione, `offset`);
- le **variabili calcolate**: `Power`, alias in W della potenza AC di uscita
  usato per l'integrale di energia e per il grafico, e le somme per i valori a
  32 bit con registri non consecutivi (`<nome> lo` + `<nome> hi`).

Il preset applicato resta visibile (in sola lettura) nella scheda del device;
blocchi e variabili si possono poi modificare a mano. Ogni file indica in
`source` progetto, licenza e commit di origine e in `notes` i prefissi del
numero di serie dei modelli coperti. **Prima di usare un preset su un impianto
verifica i valori** con la documentazione del costruttore: le mappe vengono
da progetti open source per Home Assistant e non sono state provate qui su
dispositivi reali.

I file vengono generati da `tools/build_presets.py` a partire da
[ha-solarman](https://github.com/davidrapan/ha-solarman),
[home_assistant_solarman](https://github.com/StephanJoubert/home_assistant_solarman)
e [homeassistant-solax-modbus](https://github.com/wills106/homeassistant-solax-modbus).
Per aggiornarli (sull'host, serve Python ≥ 3.11 con `pyyaml` e `pymodbus>=3.8`):

```bash
git clone --depth 1 https://github.com/davidrapan/ha-solarman /tmp/up/ha-solarman
git clone --depth 1 https://github.com/StephanJoubert/home_assistant_solarman /tmp/up/home_assistant_solarman
git clone --depth 1 https://github.com/wills106/homeassistant-solax-modbus /tmp/up/homeassistant-solax-modbus
python tools/build_presets.py --solarman /tmp/up/ha-solarman \
    --solarman-legacy /tmp/up/home_assistant_solarman \
    --solax /tmp/up/homeassistant-solax-modbus
docker compose restart web   # i preset vengono letti una volta all'avvio
```

Vengono importate solo le misure numeriche; sono esclusi impostazioni
scrivibili, stringhe, enum, bit e maschere, valori calcolati in Python e
sensori composti.

### Intervalli di acquisizione

- Telegraf: `--interval` di `export_telegraf_config` (predefinito `30s`,
  lo stesso usato dal bundle).
- Celery: `CELERY_BEAT_SCHEDULE_INTERVAL` (task `check_devices`) e
  `CELERY_PLANT_METRICS_INTERVAL` in `settings.py`.
- Tieni `check_devices` **uguale o poco più lungo** dell'intervallo
  Telegraf. Con un valore più corto Celery salva più volte lo stesso
  snapshot.

### Rigenera le credenziali di un gateway

Da usare se hai perso il bundle, per ruotare la password o per un gateway
creato prima della migrazione.

1. **Admin → Gateways** → seleziona il gateway.
2. Menu azioni → **Rigenera credenziali MQTT** → *Vai*.
3. Apri il dettaglio del gateway → **⬇ Scarica bundle gateway**.
4. Sul gateway sostituisci almeno `/etc/default/telegraf` (e `ca.crt` se è
   cambiato) e riavvia Telegraf. La vecchia password non funziona più.

### Eliminare un gateway

Basta eliminarlo dall'admin: il signal `post_delete` rimuove l'utente
`gw-<pk>` dal broker e riscrive l'ACL.

### Rinnovo o cambio del certificato del broker

Il certificato del server dura 825 giorni, la CA 10 anni. Se cambia l'IP del
server o il certificato è in scadenza:

```bash
# ATTENZIONE: --force sovrascrive TUTTO: CA, certificati, .env, passwd e acl
./bootstrap.sh --force
docker compose up -d --force-recreate
```

Dopo `--force` devi rimettere in `.env` le `POSTGRES_*` con cui è stato
creato il volume del DB, ridistribuire il nuovo
`ca.crt` a **tutti** i gateway e rigenerare le loro credenziali, perché
`passwd` viene ricreato con il solo `django-consumer`. Per ruotare
solo il certificato server tenendo la CA, rigeneralo a mano con i comandi
openssl dello step 4 di `bootstrap.sh`, usando la `ca.key` esistente.

### Monitoraggio e log

```bash
docker compose logs -f mqtt_consumer      # arrivo dati MQTT
docker compose logs -f celery             # elaborazione e salvataggio
docker compose logs -f mosquitto          # connessioni gateway, errori auth/TLS
docker compose logs -f mosquitto-admin    # provisioning credenziali
docker compose logs -f web                # Django

# Tutto il traffico MQTT
docker compose exec mosquitto mosquitto_sub -h localhost -p 1883 \
  -u django-consumer -P "$(grep MQTT_CONSUMER_PASSWORD .env | cut -d= -f2)" -t '#' -v

# Ultimo snapshot di un device: "ts" (epoch) e per ogni registro [valore, ts]
docker compose exec redis redis-cli GET mqtt:rawdata:<device_pk>
```

### Aggiornare il server

> ⚠️ **Server installati prima di questa versione.** Prima Django usava le
> credenziali DB scritte in `settings.py` e ignorava `.env`; ora usa solo le
> `POSTGRES_*` di `.env` (senza `.env` i valori predefiniti sono `postgres`,
> password vuota, DB `energy_monitoring`). Se il volume `postgres_data` è
> stato creato con i vecchi valori (utente `mac`, DB `energy-db`), **prima di
> aggiornare** metti in `.env` quegli stessi valori (utente, password e nome
> DB), altrimenti Django non si connette più.
>
> ⚠️ **PostgreSQL è fissato a `postgres:17`.** Prima era `postgres:latest`,
> che ora corrisponde alla 18. Dalla 18 l'immagine rifiuta il volume montato
> su `/var/lib/postgresql/data`, quindi il container `database` non partiva.
> Prima di aggiornare, controlla la versione sul server:
> `docker compose exec database postgres --version`. Se è già la 18, non
> tornare alla 17 sullo stesso volume: serve una migrazione dei dati
> (`pg_dump` / restore).

```bash
git pull
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py collectstatic --noinput
```

### Reset completo (solo sviluppo / test)

```bash
./reset.sh      # digita RESET per confermare
```

⚠️ Cancella container, **volumi (tutti i dati del DB)**, `.env`, `passwd`,
`acl` e certificati. Dopo il reset riparti da
[Deploy del server](#deploy-del-server). **Mai in produzione.**

---

## Troubleshooting

| Sintomo | Causa probabile | Soluzione |
|---|---|---|
| `web` / `celery` non si connettono al DB (`password authentication failed`, `database ... does not exist`) | `POSTGRES_*` in `.env` cambiati dopo che il volume `postgres_data` era già stato creato | Rimetti in `.env` i valori con cui è stato creato il volume. Solo su ambienti di test: `docker compose down -v` (cancella i dati!) e riavvia |
| `Bad Request (400)` aprendo l'admin | Host non presente in `ALLOWED_HOSTS` | Aggiungilo in `settings.py` |
| `mosquitto` in `Restarting` | Mancano `passwd`, `acl` o certificati | `./bootstrap.sh`, poi `docker compose logs mosquitto` |
| Il pulsante bundle dà errore "CA certificate non trovato" | `mosquitto/certs/ca.crt` assente | Lancia il bootstrap, poi `docker compose restart web` |
| Il bundle dice "password già rivelata" | Il bundle è già stato scaricato una volta | Azione **Rigenera credenziali MQTT** |
| Il gateway non ha la sezione bundle / nessuna credenziale | Gateway creato prima della migrazione | Azione **Rigenera credenziali MQTT** |
| Telegraf: `x509: certificate is not valid for ...` | `MQTT_SERVER` usa un IP/host diverso dal SAN del certificato | Usa l'IP passato al bootstrap, oppure rigenera i certificati |
| Telegraf: `not authorized` / `bad user name or password` | Credenziali vecchie o provisioning fallito | Controlla i log di `mosquitto-admin` e rigenera le credenziali |
| Messaggi arrivano, ma nei log di Celery c'è `No MQTT data yet` / `Stale MQTT data` | Orologio del gateway sbagliato o Telegraf fermo da più di 5 min | Controlla NTP sul gateway e `systemctl status telegraf` |
| Messaggi in cache, ma nessun `MQTT cache hit` | Il gateway non è in `protocol_mode=mqtt` | Imposta `Protocol mode = MQTT` sul gateway |
| Telegraf: errori Modbus / `connection refused` | `mbusd` non in ascolto su `127.0.0.1:<port>` oppure `slave_id` sbagliato | Verifica `mbusd` e i campi del Device, poi rigenera `telegraf.conf` |
| `export_telegraf_config`: "Nessun device modbus abilitato" | Nessun Device con `protocol=modbus` e `is_enabled=True` | Crea o abilita i dispositivi |
| Il signal di provisioning logga `MQTT provisioning failed` | `mosquitto-admin` giù o token errato | `docker compose ps`, verifica `MOSQUITTO_ADMIN_TOKEN` in `.env`, poi rigenera le credenziali |

---

## Sicurezza

- **TLS obbligatorio** per i gateway (porta 8883, TLS 1.2+); la porta 1883
  senza TLS non è pubblicata sull'host ed è raggiungibile solo dalla rete
  docker.
- **Credenziali per gateway** e **ACL**: `gw-<pk>` può scrivere e leggere solo
  `plants/<pk>/#`; `django-consumer` può solo leggere `plants/#`.
  `allow_anonymous false`.
- Password MQTT generate con `secrets.token_urlsafe(32)`. Il DB le conserva in
  chiaro **solo fino al primo download** del bundle; Mosquitto conserva solo
  l'hash PBKDF2.
- `mosquitto-admin` non espone porte, richiede un token Bearer
  (`MOSQUITTO_ADMIN_TOKEN`, 32 byte) e monta il Docker socket **in sola
  lettura** solo per inviare SIGHUP.
- Sul gateway `/etc/default/telegraf` deve avere modo `600`.
- **Segreti fuori da git**: `.env`, `mosquitto/config/passwd`,
  `mosquitto/certs/*`, `mosquitto/data/` e `mosquitto/log/` sono in
  `.gitignore`.
- Da valutare prima di esporre il server fuori dalla VPN: `DEBUG = True` in
  `settings.py`; `web` usa `runserver`; le porte
  `5432` e `6379` sono pubblicate sull'host, quindi limitale con il firewall.

---

## Sviluppo e test

I nomi host dei servizi (`database`, `redis`, `mosquitto`, `mosquitto-admin`)
sono scritti nel codice, quindi comandi e test vanno eseguiti **dentro i
container**.

```bash
docker compose exec web python manage.py test user_devices                    # tutti
docker compose exec web python manage.py test user_devices.tests.test_mqtt    # un modulo

docker compose exec web coverage run --source=user_devices.functions python manage.py test user_devices.tests.test_core_funcs
docker compose exec web coverage report -m
```

Management command utili (`user_devices/management/commands/`):

| Comando                                                            | Descrizione                                             |
|--------------------------------------------------------------------|---------------------------------------------------------|
| `export_telegraf_config <gateway_pk> [--out FILE] [--interval 30s]` | Genera il `telegraf.conf` di un gateway dal DB          |
| `generate_fake_data`                                               | Genera dati finti per test e demo                       |
| `test_midnight_aggregation`                                        | Esegue l'aggregazione di mezzanotte a mano              |

---

## Struttura del repository

```
energy_monitoring/            # progetto Django (settings, celery.py con beat schedule)
user_devices/
├── models.py                 # Gateway (+protocol_mode), Device, ModbusReadBlock, variabili, dati, GatewayMqttCredentials
├── tasks.py                  # scan_and_read_devices: cache MQTT / Modbus TCP / DLMS
├── functions.py              # map/compute variabili ed energia, lettura Modbus e DLMS
├── signals.py                # propagazione utenti + provisioning MQTT su Gateway
├── admin.py, admin_mqtt.py   # admin, pulsante bundle, azione "Rigenera credenziali"
├── mqtt/
│   ├── consumer.py           # consumer MQTT → Redis
│   ├── cache.py              # cache registri grezzi (TTL 15 min, freschezza 5 min)
│   └── admin_client.py       # client REST per mosquitto-admin
├── presets/                  # preset di mappatura (inverter/*.json) e apply_preset
└── management/commands/      # export_telegraf_config, generate_fake_data, ...
tools/build_presets.py        # genera i preset dai progetti open source
mosquitto/
├── config/mosquitto.conf     # listener 1883 (interno) e 8883 (TLS), ACL
├── config/passwd, acl        # generati da bootstrap / mosquitto-admin
└── certs/                    # CA e cert server generati da bootstrap
mosquitto-admin/              # helper FastAPI (passwd, acl, SIGHUP)
gateway_setup/
├── install_telegraf.sh       # installazione Telegraf sul gateway
└── telegraf.env.example      # template variabili d'ambiente Telegraf
bootstrap.sh                  # configurazione iniziale del server
reset.sh                      # reset completo (distruttivo)
docker-compose.yml            # 8 servizi
```

Documentazione storica: `README_FINAL.md` (guida operativa della migrazione,
che fa ancora riferimento all'applicazione di una patch), `MIGRATION.md`,
`WEBADMIN.md`.
