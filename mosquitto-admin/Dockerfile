# Debian fissata: da trixie il CLI Docker è nel pacchetto separato docker-cli
FROM python:3.11-slim-trixie

# Docker CLI per esegure SIGHUP al container mosquitto.
# Versione apt va benissimo, non serve l'engine completo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
