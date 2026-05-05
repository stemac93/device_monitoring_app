#!/usr/bin/env bash
# Installa Telegraf su un gateway Debian/Ubuntu/RPi OS.
# Uso (dal gateway):
#   sudo bash install_telegraf.sh
#
# Poi copia telegraf.conf generato dal server con:
#   python manage.py export_telegraf_config <gateway_pk> --out /tmp/telegraf.conf
#   scp /tmp/telegraf.conf user@gateway:/etc/telegraf/telegraf.conf
# e copia anche il CA cert + env file:
#   scp mosquitto/certs/ca.crt user@gateway:/etc/telegraf/ca.crt
#   scp gateway_setup/telegraf.env user@gateway:/etc/default/telegraf

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Eseguimi come root (sudo)" >&2
  exit 1
fi

# Repo ufficiale InfluxData
if [[ ! -f /etc/apt/sources.list.d/influxdata.list ]]; then
  apt-get update
  apt-get install -y gnupg curl ca-certificates
  curl -sL https://repos.influxdata.com/influxdata-archive_compat.key \
    | gpg --dearmor \
    | tee /usr/share/keyrings/influxdata-archive-keyring.gpg > /dev/null
  echo "deb [signed-by=/usr/share/keyrings/influxdata-archive-keyring.gpg] https://repos.influxdata.com/debian stable main" \
    > /etc/apt/sources.list.d/influxdata.list
fi

apt-get update
apt-get install -y telegraf

# Crea cartelle log/conf se mancanti
mkdir -p /var/log/telegraf /etc/telegraf
chown telegraf:telegraf /var/log/telegraf

systemctl enable telegraf

cat <<'EOF'

====== Installazione base completata ======

Passi rimanenti (da fare a mano, dopo aver copiato i file dal server):

1. Copia la config generata lato server:
     scp utente@server:/path/to/telegraf.conf /etc/telegraf/telegraf.conf

2. Copia il CA certificate del broker:
     scp utente@server:/path/to/mosquitto/certs/ca.crt /etc/telegraf/ca.crt

3. Configura le credenziali MQTT:
     cp /path/to/telegraf.env.example /etc/default/telegraf
     # poi edita le password

4. Avvia:
     systemctl restart telegraf
     systemctl status telegraf
     journalctl -u telegraf -f

5. Verifica dal server:
     mosquitto_sub -h localhost -p 1883 -u django-consumer -P '<pw>' \
       -t 'plants/+/devices/+/raw' -v

EOF
