# Mosquitto bootstrap

Questa cartella è montata in `/mosquitto/*` dentro il container. Prima del primo avvio devi:

## 1. Generare il file `passwd`

```bash
# crea la password per il consumer Django
docker compose run --rm mosquitto \
  mosquitto_passwd -c /mosquitto/config/passwd django-consumer
# (ti chiede la password)

# aggiungi un utente per il gateway 1
docker compose run --rm mosquitto \
  mosquitto_passwd -b /mosquitto/config/passwd gw-1 'UNA_PASSWORD_FORTE'
```

Il `-c` crea il file (usa solo la prima volta), `-b` aggiunge utente da riga
di comando, senza `-b` è interattivo.

## 2. Popolare `.env` del progetto

Aggiungi in `.env` (o `docker-compose` environment) del server:

```
MQTT_CONSUMER_USER=django-consumer
MQTT_CONSUMER_PASSWORD=lastessa-password-scelta-sopra
```

## 3. Certificati TLS — scegli A oppure B

### A) Self-signed con CA locale (semplice, nessun dominio richiesto)

```bash
cd mosquitto/certs

# CA
openssl req -new -x509 -days 3650 -extensions v3_ca \
  -keyout ca.key -out ca.crt \
  -subj "/CN=SMEL Local CA"

# Server
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr \
  -subj "/CN=mqtt.example.com"   # <-- il DNS/IP pubblico del broker
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key \
  -CAcreateserial -out server.crt -days 825 -sha256
rm server.csr

chmod 644 ca.crt server.crt
chmod 600 server.key ca.key
```

**IMPORTANTE**: copia `ca.crt` su ciascun gateway e configuralo in
`MQTT_TLS_CA` nell'env di Telegraf (vedi `gateway_setup/telegraf.env.example`).

### B) Let's Encrypt (richiede dominio pubblico + porta 80)

Usa certbot sul server. Esempio con certbot standalone:

```bash
sudo certbot certonly --standalone -d mqtt.example.com
```

Poi copia/symlinka in `mosquitto/certs/`:

```bash
ln -s /etc/letsencrypt/live/mqtt.example.com/fullchain.pem mosquitto/certs/server.crt
ln -s /etc/letsencrypt/live/mqtt.example.com/privkey.pem mosquitto/certs/server.key
# rimuovi tls_ca dalla config mosquitto o punta alla CA di Let's Encrypt
```

Mosquitto va ricaricato ogni volta che i cert si rinnovano:

```bash
docker compose exec mosquitto kill -HUP 1
```

Puoi automatizzarlo con un deploy-hook di certbot.

## 4. Ricarica dopo ogni modifica

```bash
docker compose exec mosquitto kill -HUP 1
```

o più brutalmente:

```bash
docker compose restart mosquitto
```
