# Migrazione da polling Modbus/VPN a MQTT

Questo documento descrive la procedura di switch dal vecchio sistema
(Django → VPN → mbusd → RS485) al nuovo (Telegraf → MQTT → Mosquitto →
consumer Django → cache Redis → Celery beat → DB).

## Architettura risultante

```
┌─────────────── GATEWAY (RPi) ─────────────┐        ┌────────── SERVER ───────────┐
│                                            │        │                              │
│  mbusd (invariato) ──► 127.0.0.1:503       │        │  Mosquitto 8883/TLS         │
│                         127.0.0.1:504      │        │       ▲                      │
│                          ▲                 │ MQTTS  │       │                      │
│                          │                 │ ◄─────►│  mqtt_consumer              │
│                       Telegraf             │  TLS   │       │                      │
│                   (inputs.modbus.request   │        │       ▼                      │
│                    + outputs.mqtt)         │        │  Redis cache (raw)          │
│                                            │        │       ▲                      │
└────────────────────────────────────────────┘        │       │                      │
                                                      │  Celery beat/worker         │
                                                      │  scan_and_read_devices       │
                                                      │       │                      │
                                                      │       ▼                      │
                                                      │  PostgreSQL                  │
                                                      │  (DeviceData, EnergyData)    │
                                                      └──────────────────────────────┘
```

**Cosa cambia e cosa no**
- `models.py`: **nessuna modifica, nessuna migrazione di schema**
- `functions.py`: invariato (continua a mappare `base_values` → variabili)
- Il ramo DLMS di `scan_and_read_devices`: invariato, continua a fare polling
- Il ramo Modbus di `scan_and_read_devices`: **nuovo**, legge da cache Redis invece che TCP

## Prerequisiti

Server:
- Docker + docker compose
- Un nome DNS pubblico **O** un IP raggiungibile dai gateway via VPN
- Porta 8883 raggiungibile dai gateway (la VPN esistente va benissimo)

Gateway (per ogni impianto):
- Accesso SSH root
- Debian/Ubuntu/RPi OS
- mbusd già in esecuzione su porte 503/504 (situazione attuale, non toccare)
- Connettività verso il broker sul server

---

## Procedura di cutover

### Fase 1 — Preparazione server (senza impatto su produzione)

1. **Merge della patch** nella fork:
   ```bash
   git checkout -b feature/mqtt-migration
   # applica i file di questo patch-set
   git add .
   git commit -m "feat: migration from direct Modbus polling to MQTT pipeline"
   ```

2. **Genera le credenziali Mosquitto**:
   ```bash
   cd mosquitto
   # Avvia temporaneamente mosquitto per usare mosquitto_passwd
   docker compose up -d mosquitto

   # Utente per il consumer Django
   docker compose exec mosquitto \
     mosquitto_passwd -c /mosquitto/config/passwd django-consumer
   # password a tua scelta, segnatela

   # Un utente per ogni gateway esistente. Esempio per pk=1:
   docker compose exec mosquitto \
     mosquitto_passwd -b /mosquitto/config/passwd gw-1 'PASSWORD_GW_1'
   ```

3. **Personalizza l'ACL** in `mosquitto/config/acl` con i PK reali dei Gateway
   e rimuovi il blocco commentato del gw-2 se hai solo un gateway, oppure
   aggiungi un blocco per ogni gateway reale. Ricarica:
   ```bash
   docker compose exec mosquitto kill -HUP 1
   ```

4. **Genera i certificati TLS** (self-signed, vedi `mosquitto/README.md` per
   la procedura Let's Encrypt alternativa):
   ```bash
   cd mosquitto/certs
   openssl req -new -x509 -days 3650 -extensions v3_ca \
     -keyout ca.key -out ca.crt \
     -subj "/CN=SMEL Local CA"
   openssl genrsa -out server.key 2048
   openssl req -new -key server.key -out server.csr \
     -subj "/CN=mqtt.example.com"   # <-- il tuo dominio o IP
   openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key \
     -CAcreateserial -out server.crt -days 825 -sha256
   chmod 600 server.key ca.key
   ```

5. **Aggiungi le env MQTT** al `.env` del progetto (stessa cartella di
   `docker-compose.yml`):
   ```
   MQTT_CONSUMER_USER=django-consumer
   MQTT_CONSUMER_PASSWORD=la-password-scelta-al-passo-2
   ```

6. **Build e avvio completo** (tutto sullo stesso host):
   ```bash
   docker compose build
   docker compose up -d
   docker compose logs -f mqtt_consumer
   ```
   Il consumer tenta la connessione a mosquitto:1883. Se vedi
   `Connected to MQTT broker` sei a posto.

7. **Verifica che il ramo DLMS continui a funzionare** (se hai device DLMS):
   ```bash
   docker compose logs -f celery | grep DLMS
   ```

A questo punto il server è **pronto** ma non riceve ancora nulla: nessun
Telegraf sta pubblicando. Il Celery task `scan_and_read_devices` loggerà
`No MQTT data yet for device X` per tutti i Modbus — normale.

---

### Fase 2 — Configurazione di UN gateway di test

Scegli un gateway con cui iniziare. Per il resto della procedura sostituisci
`<GW_PK>` con il PK effettivo e `user@GATEWAY_IP` con le credenziali SSH.

1. **Genera la config Telegraf lato server**:
   ```bash
   docker compose exec web \
     python manage.py export_telegraf_config <GW_PK> \
     --out /tmp/telegraf-<GW_PK>.conf --interval 30s

   # copia in locale
   docker compose cp web:/tmp/telegraf-<GW_PK>.conf ./telegraf-<GW_PK>.conf
   ```

2. **Verifica la config** prima di inviarla al gateway:
   ```bash
   grep "\[\[inputs.modbus\]\]" telegraf-<GW_PK>.conf | wc -l
   # Deve corrispondere al numero di device Modbus abilitati del gateway
   ```

3. **Copia tutto sul gateway**:
   ```bash
   # Certificato CA
   scp mosquitto/certs/ca.crt user@GATEWAY_IP:/tmp/

   # Config Telegraf
   scp telegraf-<GW_PK>.conf user@GATEWAY_IP:/tmp/

   # Env file
   cp gateway_setup/telegraf.env.example /tmp/telegraf.env
   # edita /tmp/telegraf.env con: MQTT_SERVER, MQTT_USERNAME=gw-<GW_PK>, MQTT_PASSWORD, MQTT_TLS_CA=/etc/telegraf/ca.crt
   scp /tmp/telegraf.env user@GATEWAY_IP:/tmp/

   # Script installazione
   scp gateway_setup/install_telegraf.sh user@GATEWAY_IP:/tmp/
   ```

4. **Sul gateway**:
   ```bash
   ssh user@GATEWAY_IP
   sudo bash /tmp/install_telegraf.sh

   sudo cp /tmp/ca.crt /etc/telegraf/ca.crt
   sudo cp /tmp/telegraf-*.conf /etc/telegraf/telegraf.conf
   sudo cp /tmp/telegraf.env /etc/default/telegraf
   sudo chown telegraf:telegraf /etc/telegraf/ca.crt
   sudo chmod 600 /etc/default/telegraf

   sudo systemctl restart telegraf
   sudo journalctl -u telegraf -f
   ```

5. **Verifica lato gateway** (Telegraf deve:
   - connettersi a mbusd 127.0.0.1:503/504 → "successfully connected"
   - leggere i registri → metriche `modbus` nei log
   - pubblicare MQTT → "mqtt: connected" e nessun errore

6. **Verifica lato server**:
   ```bash
   # I messaggi MQTT arrivano?
   docker compose exec mosquitto \
     mosquitto_sub -h localhost -p 1883 \
     -u django-consumer -P '<PW>' \
     -t 'plants/+/devices/+/raw' -v

   # La cache Redis si popola?
   docker compose exec redis redis-cli KEYS 'mqtt:rawdata:*'
   docker compose exec redis redis-cli GET 'mqtt:rawdata:<DEVICE_PK>'

   # Celery beat/worker processa?
   docker compose logs -f celery | grep "Cache hit for device"
   ```

7. **Verifica i dati in DB**:
   Dopo 1-2 minuti il DeviceData dovrebbe avere nuove righe con i dati
   provenienti da MQTT. I valori devono essere **identici** a quelli che il
   vecchio polling diretto produceva.

---

### Fase 3 — Rollout sugli altri gateway

Ripeti Fase 2 per ogni gateway. Ogni gateway è indipendente: puoi
migrarli uno alla volta senza impatto.

Se vuoi generare tutte le config in un colpo:
```bash
for pk in $(docker compose exec -T web python manage.py shell -c \
     'from user_devices.models import Gateway; print("\n".join(str(g.pk) for g in Gateway.objects.all()))'); do
  docker compose exec web python manage.py export_telegraf_config $pk \
    --out /tmp/telegraf-$pk.conf
done
```

---

### Fase 4 — Verifica finale

Dopo aver migrato tutti i gateway, controlla:

- [ ] Tutti i device Modbus abilitati hanno una chiave `mqtt:rawdata:<pk>` in Redis
- [ ] Celery non logga più `No MQTT data yet for device X`
- [ ] I grafici nella webapp mostrano dati continui dalla migrazione in poi
- [ ] `compute_plant_metrics` funziona (availability/performance/production aggiornati)
- [ ] `midnight_energy_aggregation` ha prodotto un record al primo mezzanotte dopo la migrazione

---

## Rollback (se qualcosa va male su un singolo gateway)

Sul gateway:
```bash
sudo systemctl stop telegraf
sudo systemctl disable telegraf
```

Questo interrompe la pubblicazione MQTT dal gateway. mbusd continua a
girare (non l'hai mai fermato). Per tornare al vecchio comportamento
**per quel gateway** dovresti però anche ripristinare il vecchio
`tasks.py` con polling TCP diretto, il che significa redeploy del server
intero — non è un rollback "chirurgico" per-gateway con questo patch-set.

Per evitare questo scenario, suggerisco di tenere aperta una finestra
di monitoraggio attivo durante le prime 24-48h post-cutover, e di
migrare gateway-per-gateway con pausa di almeno mezz'ora tra uno e l'altro
per avere tempo di accorgersi di problemi prima che si propaghino.

---

## Cosa tenere d'occhio

1. **Clock drift**: Telegraf mette il timestamp della lettura nel payload.
   Se il clock del gateway è fuori sync, la cache lato server potrebbe
   marcare tutto come stale. Assicurati che NTP sia attivo su ogni gateway.

2. **Retained messages**: con `retain=true`, Mosquitto conserva l'ultimo
   valore di ogni topic sul disco. Se resetti l'ACL o cambi username per
   un gateway, i retained vecchi restano. Per pulire:
   ```bash
   mosquitto_pub -h localhost -u django-consumer -P '<PW>' \
     -t 'plants/<GW>/devices/<DEV>/raw' -r -n
   ```

3. **Numero di `[[inputs.modbus]]` per gateway**: se il gateway ha molti
   device (diciamo >20), Telegraf apre altrettante connessioni Modbus TCP
   simultanee verso mbusd. mbusd è single-threaded sul bus RS485 quindi
   le letture si serializzano comunque, ma se noti timeout frequenti
   aumenta `timeout = "5s"` nella config generata (modifica il template
   nel management command).

4. **Il tuo edge case noto sulla priority proxy**: se prima o poi sostituirai
   mbusd con il `modbus-priority-proxy.py` del SMEL-BPI-6204-485-BRIDGE,
   il "Connection reset by peer" che già stai investigando si manifesterà
   anche qui — Telegraf è un TCP client come qualsiasi altro.
