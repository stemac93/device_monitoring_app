#!/usr/bin/env bash
#
# bootstrap.sh — configurazione iniziale del server MQTT.
#
# Crea: .env, mosquitto/config/passwd, mosquitto/config/acl,
#       certificati TLS self-signed in mosquitto/certs/.
#
# Uso:
#   ./bootstrap.sh                     # interattivo, ti chiede tutto
#   ./bootstrap.sh --non-interactive   # legge tutto da env vars (vedi sotto)
#
# Variabili d'ambiente accettate (sovrascrivono i prompt):
#   POSTGRES_PASSWORD
#   MQTT_CONSUMER_PASSWORD
#   SERVER_VPN_IP                      # es: 10.8.0.1
#   GATEWAY_PKS                        # CSV es: "1,2,3"
#   GATEWAY_PASSWORDS                  # CSV nello stesso ordine es: "pwgw1,pwgw2,pwgw3"
#                                      # (se omesso, ne genera di random)
#
# Idempotenza:
# - Se un file (.env, passwd, certs) esiste già, lo script NON lo sovrascrive
#   senza conferma. Usa --force per ricrearli da zero.
#
# Cosa NON fa lo script:
# - Non avvia docker compose (lo fai tu dopo).
# - Non fa migrate Django (lo fai tu dopo `docker compose up -d`).
# - Non installa pacchetti di sistema.

set -euo pipefail

# ----- helpers -----
RED=$(tput setaf 1 2>/dev/null || true)
GREEN=$(tput setaf 2 2>/dev/null || true)
YELLOW=$(tput setaf 3 2>/dev/null || true)
BLUE=$(tput setaf 4 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)

log()  { echo "${BLUE}[bootstrap]${RESET} $*"; }
ok()   { echo "${GREEN}[ok]${RESET} $*"; }
warn() { echo "${YELLOW}[warn]${RESET} $*" >&2; }
err()  { echo "${RED}[err]${RESET} $*" >&2; }
die()  { err "$@"; exit 1; }

prompt_secret() {
    # prompt_secret VAR_NAME "Prompt text"
    local varname=$1
    local message=$2
    local current=${!varname:-}
    if [[ -n $current ]]; then
        return 0
    fi
    if [[ ${INTERACTIVE:-1} -eq 0 ]]; then
        die "Variabile $varname non impostata e --non-interactive: imposta env e riprova."
    fi
    local pw1 pw2
    while true; do
        read -srp "$message: " pw1; echo
        if [[ -z $pw1 ]]; then
            warn "Vuota, riprova."
            continue
        fi
        read -srp "Conferma: " pw2; echo
        if [[ $pw1 == "$pw2" ]]; then
            printf -v "$varname" '%s' "$pw1"
            return 0
        fi
        warn "Le password non coincidono, riprova."
    done
}

prompt_value() {
    # prompt_value VAR_NAME "Prompt text" "default"
    local varname=$1
    local message=$2
    local default=${3:-}
    local current=${!varname:-}
    if [[ -n $current ]]; then
        return 0
    fi
    if [[ ${INTERACTIVE:-1} -eq 0 ]]; then
        if [[ -n $default ]]; then
            printf -v "$varname" '%s' "$default"
            return 0
        fi
        die "Variabile $varname non impostata e --non-interactive."
    fi
    local prompt="$message"
    [[ -n $default ]] && prompt="$prompt [$default]"
    read -rp "$prompt: " value
    [[ -z $value && -n $default ]] && value=$default
    [[ -z $value ]] && die "Valore obbligatorio."
    printf -v "$varname" '%s' "$value"
}

random_password() {
    openssl rand -base64 24 | tr -d '/+=' | head -c 24
}

# ----- argparse -----
INTERACTIVE=1
FORCE=0
for arg in "$@"; do
    case $arg in
        --non-interactive) INTERACTIVE=0 ;;
        --force) FORCE=1 ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0 ;;
        *) die "Argomento sconosciuto: $arg" ;;
    esac
done

# ----- preflight -----
cd "$(dirname "$0")"

[[ -f docker-compose.yml ]] || die "Esegui lo script dalla root del progetto (manca docker-compose.yml)."

command -v docker >/dev/null || die "docker non installato."
command -v openssl >/dev/null || die "openssl non installato (apt install openssl)."

# Verifica che il daemon Docker risponda — se no probabilmente l'utente non è nel gruppo docker
if ! docker info >/dev/null 2>&1; then
    die "Docker non risponde. Sei nel gruppo 'docker'? (sudo usermod -aG docker \$USER && newgrp docker)"
fi

log "Working dir: $(pwd)"

# ----- 1. Raccolta input -----
echo
echo "=========================================="
echo " Step 1/4: raccolta credenziali"
echo "=========================================="
echo

prompt_secret POSTGRES_PASSWORD       "Password PostgreSQL (DB del container)"
prompt_secret MQTT_CONSUMER_PASSWORD  "Password MQTT consumer (django-consumer)"
prompt_value  SERVER_VPN_IP           "IP del server visto dai gateway via VPN" ""

# Validazione IP
if ! [[ $SERVER_VPN_IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    die "IP non valido: $SERVER_VPN_IP"
fi

# Gateway: con la nuova versione, i gateway si aggiungono dall'admin web.
# Le credenziali MQTT sono generate automaticamente da Django via signal.
# Quindi qui NON chiediamo nulla.

# ----- 2. .env -----
echo
echo "=========================================="
echo " Step 2/4: generazione .env"
echo "=========================================="
echo

if [[ -f .env && $FORCE -eq 0 ]]; then
    warn ".env esiste già. Salto. Usa --force per sovrascrivere."
else
    cat > .env << EOF
# Generato da bootstrap.sh il $(date -Iseconds)
# NON committare questo file. Già in .gitignore.

# --- Database PostgreSQL (container) ---
POSTGRES_USER=postgres
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=energy_monitoring

# --- MQTT consumer ---
MQTT_CONSUMER_USER=django-consumer
MQTT_CONSUMER_PASSWORD=${MQTT_CONSUMER_PASSWORD}

# --- Endpoint MQTT pubblico (incluso nel bundle Telegraf di ogni gateway) ---
# Esempio: ssl://10.8.0.1:8883
MQTT_PUBLIC_ENDPOINT=ssl://${SERVER_VPN_IP}:8883

# --- Token interno per parlare col container mosquitto-admin ---
MOSQUITTO_ADMIN_TOKEN=$(openssl rand -hex 32)
EOF
    chmod 600 .env
    ok ".env creato (modo 600)"
fi

# Assicura .gitignore
if ! grep -qxF '.env' .gitignore 2>/dev/null; then
    {
        echo ""
        echo "# Bootstrap-generated secrets"
        echo ".env"
        echo "mosquitto/config/passwd"
        echo "mosquitto/certs/*.key"
        echo "mosquitto/certs/*.crt"
        echo "mosquitto/certs/*.srl"
        echo "mosquitto/data/"
        echo "mosquitto/log/"
    } >> .gitignore
    ok "Aggiornato .gitignore"
fi

# ----- 3. Mosquitto: passwd, acl, dirs -----
echo
echo "=========================================="
echo " Step 3/4: configurazione Mosquitto"
echo "=========================================="
echo

mkdir -p mosquitto/data mosquitto/log mosquitto/certs mosquitto/config
ok "Cartelle mosquitto/* pronte"

# passwd
PASSWD_FILE=mosquitto/config/passwd
if [[ -f $PASSWD_FILE && $FORCE -eq 0 ]]; then
    warn "$PASSWD_FILE esiste già, salto. Usa --force per ricreare."
else
    rm -f "$PASSWD_FILE"
    touch "$PASSWD_FILE"

    # Solo django-consumer al bootstrap. I gateway vengono aggiunti dall'admin web,
    # via Django signal → mosquitto-admin → mosquitto_passwd.
    docker run --rm \
        -v "$(pwd)/mosquitto/config:/mosquitto/config" \
        eclipse-mosquitto:2 \
        mosquitto_passwd -b /mosquitto/config/passwd django-consumer "$MQTT_CONSUMER_PASSWORD" \
        >/dev/null

    ok "Generato $PASSWD_FILE con utente django-consumer (i gateway si aggiungono dall'admin web)"
fi

# acl
ACL_FILE=mosquitto/config/acl
if [[ -f $ACL_FILE && $FORCE -eq 0 && $(wc -l < "$ACL_FILE") -gt 5 ]]; then
    # se il file ha più di 5 righe presumo sia già personalizzato dall'utente
    warn "$ACL_FILE sembra già personalizzato, salto. Usa --force per sovrascrivere."
else
    {
        echo "# Generato da bootstrap.sh $(date -Iseconds)"
        echo "# Solo django-consumer al bootstrap."
        echo "# I blocchi gw-N vengono aggiunti automaticamente da Django via mosquitto-admin"
        echo "# quando crei un Gateway dall'admin web."
        echo
        echo "user django-consumer"
        echo "topic read plants/#"
        echo
    } > "$ACL_FILE"
    ok "Generato $ACL_FILE con consumer-only (gateway aggiunti dinamicamente)"
fi

# ----- 4. Certificati TLS -----
echo
echo "=========================================="
echo " Step 4/4: certificati TLS self-signed"
echo "=========================================="
echo

CERTS_DIR=mosquitto/certs
if [[ -f $CERTS_DIR/server.crt && $FORCE -eq 0 ]]; then
    warn "Certificati già presenti in $CERTS_DIR/, salto. Usa --force per ricrearli."
    log "Suggerimento: --force ricrea anche la CA, quindi i ca.crt copiati sui gateway non sono più validi."
else
    pushd "$CERTS_DIR" >/dev/null

    # CA
    openssl req -new -x509 -days 3650 -extensions v3_ca \
        -keyout ca.key -out ca.crt \
        -subj "/CN=SMEL Local CA" -nodes 2>/dev/null

    # Server key
    openssl genrsa -out server.key 2048 2>/dev/null

    # Extensions con SAN (CRITICO per paho-mqtt)
    cat > server.ext << EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment
subjectAltName = @alt_names
[alt_names]
IP.1  = ${SERVER_VPN_IP}
EOF

    openssl req -new -key server.key -out server.csr -subj "/CN=${SERVER_VPN_IP}" 2>/dev/null
    openssl x509 -req -in server.csr \
        -CA ca.crt -CAkey ca.key -CAcreateserial \
        -out server.crt -days 825 -sha256 \
        -extfile server.ext 2>/dev/null

    rm -f server.csr server.ext ca.srl
    chmod 644 ca.crt server.crt
    chmod 600 server.key ca.key

    popd >/dev/null

    # Verifica SAN
    if openssl x509 -in "$CERTS_DIR/server.crt" -noout -text \
        | grep -q "IP Address:${SERVER_VPN_IP}"; then
        ok "Certificati generati con SAN IP=${SERVER_VPN_IP}"
    else
        die "Certificato generato ma SAN non corretto. Controlla manualmente."
    fi
fi

# ----- riepilogo -----
echo
echo "=========================================="
echo " ${GREEN}Bootstrap completato${RESET}"
echo "=========================================="
echo
echo "File generati:"
echo "  .env                       (modo 600)"
echo "  mosquitto/config/passwd    ($(wc -l < $PASSWD_FILE) utenti)"
echo "  mosquitto/config/acl"
echo "  mosquitto/certs/ca.crt     ← copia questo sui gateway"
echo "  mosquitto/certs/ca.key     (CA privata, NON copiare)"
echo "  mosquitto/certs/server.crt"
echo "  mosquitto/certs/server.key"
echo

echo "Prossimi passi:"
echo "  1. ${BLUE}docker compose down${RESET}             # se avevi container vecchi"
echo "  2. ${BLUE}docker compose up -d --build${RESET}"
echo "  3. ${BLUE}docker compose ps${RESET}               # verifica tutti Up (mosquitto NON in restart)"
echo "  4. ${BLUE}docker compose exec web python manage.py migrate${RESET}"
echo "  5. ${BLUE}docker compose exec web python manage.py createsuperuser${RESET}"
echo "  6. ${BLUE}docker compose exec web python manage.py collectstatic --noinput${RESET}"
echo
echo "Aggiungere un nuovo gateway:"
echo "  - Apri http://<server>:8000/admin/ → User devices → Gateways → Add"
echo "  - Inserisci nome e IP (l'IP è quello via VPN del gateway)"
echo "  - Salva: la password MQTT viene generata automaticamente"
echo "  - Apri il gateway appena creato e clicca '⬇ Scarica bundle gateway'"
echo "  - Copia il .tar.gz sul gateway, srotola, configura Telegraf (vedi README dentro il bundle)"
echo
