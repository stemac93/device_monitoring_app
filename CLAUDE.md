# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working rules (from `.cursor/rules/project-rules.mdc`)

- Explain the plan first and wait for confirmation before writing code.
- Start with the simplest valid solution.
- Reuse existing code instead of duplicating it.
- Only make the changes explicitly requested.
- Use modules / separation where applicable.

Much of the code, comments and docs are in Italian; keep new comments consistent with the surrounding file.

## Commands

Everything runs in Docker (`docker compose`, v2). Service hostnames (`database`, `redis`, `mosquitto`, `mosquitto-admin`) are hardcoded in settings/code, so Django commands and tests must run inside the containers, not on the host.

```bash
./bootstrap.sh                 # first-time setup: generates .env, mosquitto passwd/acl, self-signed TLS certs
docker compose up -d --build   # 8 services: web, database, celery, celery-beat, redis, mosquitto, mosquitto-admin, mqtt_consumer
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
./reset.sh                     # DESTRUCTIVE: drops containers, volumes (all DB data), .env, passwd/acl, certs
```

App: admin at `http://localhost:8000/admin/`, user dashboard at `http://localhost:8000/home/`.

Tests use Django's test runner (`TestCase`), not pytest:

```bash
docker compose exec web python manage.py test user_devices                          # all
docker compose exec web python manage.py test user_devices.tests.test_mqtt          # one module
docker compose exec web python manage.py test user_devices.tests.test_mqtt.ParseRegisterFieldTests.test_parses_hex_addresses  # one test
docker compose exec web coverage run --source=user_devices.functions python manage.py test user_devices.tests.test_core_funcs
docker compose exec web coverage report -m
```


Management commands in `user_devices/management/commands/`:
- `export_telegraf_config <gateway_pk> [--out ...] [--interval 30s]`: generates a gateway's `telegraf.conf` from the DB
- `generate_fake_data`, `test_midnight_aggregation`

No linter is configured.

## Architecture

Django 5.1 project `energy_monitoring` with a single app, `user_devices`. It monitors Modbus/DLMS energy devices behind Raspberry Pi gateways (solar plants: production, consumption, availability, performance, irradiance).

### Data model (`user_devices/models.py`)
- `Gateway` → many `Device` (FK field is literally named `Gateway`). Data models (`DeviceData`, `EnergyData`, `GatewayData`) store readings as JSON in `data`. `DeviceData`/`EnergyData` use FK `device_name` → `Device`.
- Modbus devices read one or more `ModbusReadBlock`s (register type, start address, word count). Migrations were squashed into a single `0001_initial` (fresh installs only).
- Per-device variable definitions: `ModbusMappingVariable` (register type, register address, bit length, signedness, endianness, conversion factor, offset: `(raw - offset) * factor`), `DlmsMappingVariable` (OBIS code), `ComputedVariable` (formula over other variable names). Formulas are evaluated by `helper_funcs.evaluate_formula` (AST whitelist, not sanitized): variable names inside formulas must already use `_` instead of spaces and `-`. All three share the abstract `DeviceVariable`; only one variable per device can have `show_on_graph`.
- Every model has a `user` M2M; `signals.sync_users_to_devices_and_data` propagates a Gateway's users to its devices and data (per-user visibility).
- `Button`: GPIO pins on the gateway, toggled over SSH (`commands.py`, paramiko).

### Ingestion pipeline (chosen per gateway by `Gateway.protocol_mode`: `mqtt`, `modbus_direct`, `dlms`)
Celery beat (`energy_monitoring/celery.py`) runs `check_all_devices` → a `group` of `scan_and_read_devices(gateway_pk)` tasks, one per gateway, each guarded by a non-blocking Redis lock. For each enabled device:
- **`modbus_direct` (legacy)**: direct Modbus TCP polling of the gateway's `mbusd` via pymodbus (`functions.read_modbus_registers`).
- **`mqtt`**: reads the latest raw registers from Redis (`mqtt/cache.py`, key `mqtt:rawdata:{device_pk}`, discarded if older than `RAW_FRESHNESS_SECONDS`).
- **`dlms`**: gateway with DLMS meters only (no MQTT credentials; DeviceForm rejects Modbus devices). DLMS devices are polled directly in every mode.

Both Modbus paths produce `base_values` `{(register_type, int_addr): raw_int}` and then go through the same code: `functions.map_variables` → `compute_variables` → `compute_energy` → `store_*_in_database`. `compute_plant_metrics` (DB-only aggregation into `GatewayData`) and `midnight_energy_aggregation` also run on beat.

The MQTT path: Telegraf on the gateway reads mbusd and publishes to `plants/{gateway_pk}/devices/{device_pk}/raw`, one field per register named `ir_0xNNNN` (input) or `hr_0xNNNN` (holding); one Telegraf request per read block. Mosquitto (TLS 8883 for gateways, plain 1883 inside the Docker network) passes these to `mqtt_consumer` (`python -m user_devices.mqtt.consumer`, a long-running paho process), which only fills the Redis cache and never writes to the DB (it ignores retained messages and trusts the topic, not the payload tags). The cache merges messages and keeps a timestamp per register; Celery uses `claim_raw`, which returns each snapshot once and drops stale registers. Persistence stays with Celery.

### MQTT credential provisioning
- A `post_save` on a new `Gateway` (`signals.py`) creates `GatewayMqttCredentials` (username `gw-{pk}`, random password) and calls the `mosquitto-admin` helper through `mqtt/admin_client.py`. It then rewrites the full ACL from the DB (each gateway gets `rw plants/{pk}/#`; `django-consumer` gets read on `plants/#`) and SIGHUPs the broker. Switching a gateway out of `mqtt` deletes its credentials and broker user; switching back provisions new ones. `post_delete` removes the user.
- `mosquitto-admin/` is a separate FastAPI container (Bearer `MOSQUITTO_ADMIN_TOKEN`, internal network only). It edits `mosquitto/config/passwd` and `acl` and signals the broker through the mounted Docker socket.
- `admin_mqtt.py` provides the admin view that downloads the gateway bundle (`telegraf.conf` generated by `export_telegraf_config`, `ca.crt`, `telegraf.env`, README). The plaintext password is shown only once (`reveal_once`); after that you need the "Rigenera credenziali MQTT" admin action. `admin.py` imports `admin_mqtt` for its registrations.
- `mosquitto/config/passwd`, `acl`, `mosquitto/certs/*` and `.env` are generated by `bootstrap.sh`; don't hand-edit them as source.

### Device presets
- `user_devices/presets/inverter/*.json`: Modbus maps generated by `tools/build_presets.py` (runs on the host, not in Docker) from ha-solarman, home_assistant_solarman and homeassistant-solax-modbus. Don't hand-edit them; regenerate them instead.
- The `apply_preset` field of `DeviceForm` appears only on the add form. `DeviceAdmin.save_model` calls `presets.apply_preset`, which creates blocks, variables and a computed `Pout` alias (kW, `max(0, …)`), then stores the id in `Device.preset`. `compute_energy` treats `Power`/`P` as signed power (negative = produced) and `Pout`/`Pin` as produced/consumed; plant production sums `Pout` in kW, so presets must never contain variables named like those (the generator prefixes them with `Inverter `). Planned next categories: solarimeters, anemometers, energy meters (reference: devices supported by Solar-Log).

### Other notes
- `settings.py` reads the Postgres credentials from `POSTGRES_*` (passed from `.env` by docker-compose to `database` and all Django containers), with generic defaults (`postgres`, empty password, `energy_monitoring`) when unset. `DEBUG = True` is hardcoded.
- Beat intervals: `CELERY_BEAT_SCHEDULE_INTERVAL` / `CELERY_PLANT_METRICS_INTERVAL` in settings. Keep `check_devices` at or slightly above the Telegraf interval (default 30s).
- `README_FINAL.md` is the authoritative MQTT deployment/operations guide (Italian). `MIGRATION.md` and `WEBADMIN.md` are historical.
