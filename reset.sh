#!/usr/bin/env bash
#
# reset.sh — ripulisce lo stato Docker per ripartire da zero.
#
# ATTENZIONE: rimuove tutti i container, volumi e file generati dal bootstrap.
# Va usato solo su ambienti di sviluppo/test, MAI in produzione.
#
# Cosa fa:
#   1. docker compose down -v        (rimuove container + volumi: DB e mosquitto/data)
#   2. Cancella .env, mosquitto/config/passwd, mosquitto/config/acl, mosquitto/certs/*
#
# Cosa NON tocca:
#   - Il codice del progetto
#   - .gitignore
#   - mosquitto/config/mosquitto.conf (template, va sempre tenuto)

set -euo pipefail

cd "$(dirname "$0")"

[[ -f docker-compose.yml ]] || { echo "Esegui dalla root del progetto"; exit 1; }

YELLOW=$(tput setaf 3 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)

echo "${RED}ATTENZIONE${RESET}: questo script cancella:"
echo "  - tutti i container del progetto"
echo "  - il volume postgres_data (TUTTI I DATI DJANGO)"
echo "  - il volume mosquitto data"
echo "  - .env"
echo "  - mosquitto/config/passwd, mosquitto/config/acl"
echo "  - mosquitto/certs/* (CA + cert server)"
echo
read -rp "Sei sicuro? Digita 'RESET' per confermare: " confirm
[[ $confirm == "RESET" ]] || { echo "Annullato."; exit 0; }

echo
echo "${YELLOW}1. Spengo e rimuovo container + volumi...${RESET}"
docker compose down -v 2>&1 | sed 's/^/   /'

echo "${YELLOW}2. Cancello file generati...${RESET}"
rm -fv .env
rm -fv mosquitto/config/passwd
rm -fv mosquitto/config/acl
rm -fv mosquitto/certs/{ca.crt,ca.key,server.crt,server.key,*.srl} 2>/dev/null

echo
echo "${YELLOW}Reset completato.${RESET} Ora puoi rilanciare ./bootstrap.sh per ripartire."
