"""
Test del pipeline MQTT: cache Redis e dispatcher del consumer.

Questi test NON richiedono un broker vero: simulano solo l'arrivo di un
messaggio e verificano che la cache venga popolata correttamente e che
il Celery task `_process_modbus_from_cache` la consumi.

Lanciare con:
    docker compose exec web python manage.py test user_devices.tests.test_mqtt
"""

import json
import time
from unittest.mock import patch, MagicMock

from django.test import TestCase


class ParseRegisterFieldTests(TestCase):
    def test_parses_hex_addresses(self):
        from user_devices.mqtt.consumer import _parse_register_field

        self.assertEqual(_parse_register_field("reg_0x0280"), 0x280)
        self.assertEqual(_parse_register_field("reg_0x0000"), 0)
        self.assertEqual(_parse_register_field("reg_0xffff"), 0xFFFF)

    def test_rejects_non_reg_fields(self):
        from user_devices.mqtt.consumer import _parse_register_field

        self.assertIsNone(_parse_register_field("foo"))
        self.assertIsNone(_parse_register_field("reg_xyz"))
        self.assertIsNone(_parse_register_field(""))


class HandleRawTests(TestCase):
    """Verifica che un messaggio MQTT ben formato popoli la cache."""

    @patch("user_devices.mqtt.consumer.put_raw")
    def test_valid_message_caches_registers(self, mock_put):
        from user_devices.mqtt.consumer import _handle_raw

        payload = json.dumps(
            {
                "name": "modbus",
                "timestamp": 1745161800,
                "tags": {"gateway_id": "1", "device_id": "42", "slave_id": "3"},
                "fields": {
                    "reg_0x0280": 2301,
                    "reg_0x0281": 15,
                    "reg_0x0282": 48923,
                },
            }
        ).encode("utf-8")

        _handle_raw("1", "42", payload)

        mock_put.assert_called_once()
        args, kwargs = mock_put.call_args
        self.assertEqual(args[0], 42)  # device_pk
        self.assertEqual(args[1], {0x280: 2301, 0x281: 15, 0x282: 48923})
        self.assertEqual(kwargs.get("ts"), 1745161800.0)

    @patch("user_devices.mqtt.consumer.put_raw")
    def test_malformed_json_is_silent(self, mock_put):
        from user_devices.mqtt.consumer import _handle_raw

        _handle_raw("1", "42", b"{not json")
        mock_put.assert_not_called()

    @patch("user_devices.mqtt.consumer.put_raw")
    def test_empty_payload_ignored(self, mock_put):
        from user_devices.mqtt.consumer import _handle_raw

        _handle_raw("1", "42", b"")
        mock_put.assert_not_called()

    @patch("user_devices.mqtt.consumer.put_raw")
    def test_device_id_from_tag_wins_over_topic(self, mock_put):
        """Se topic e tag divergono (improbabile ma possibile), vince il tag."""
        from user_devices.mqtt.consumer import _handle_raw

        payload = json.dumps(
            {
                "tags": {"gateway_id": "1", "device_id": "99"},
                "fields": {"reg_0x0000": 1},
            }
        ).encode("utf-8")

        _handle_raw("1", "42", payload)
        args, _ = mock_put.call_args
        self.assertEqual(args[0], 99)

    @patch("user_devices.mqtt.consumer.put_raw")
    def test_non_register_fields_filtered(self, mock_put):
        from user_devices.mqtt.consumer import _handle_raw

        payload = json.dumps(
            {
                "tags": {"gateway_id": "1", "device_id": "42"},
                "fields": {
                    "reg_0x0100": 111,
                    "not_a_register": 999,
                    "reg_0x0101": 222,
                },
            }
        ).encode("utf-8")

        _handle_raw("1", "42", payload)
        args, _ = mock_put.call_args
        self.assertEqual(args[1], {0x100: 111, 0x101: 222})


class CacheRoundtripTests(TestCase):
    """Sanity check: quello che metto nella cache lo rileggo uguale."""

    def setUp(self):
        # Patch il redis module-level per evitare connessioni reali
        self.fake_store = {}

        def fake_set(key, value, ex=None):
            self.fake_store[key] = value
            return True

        def fake_get(key):
            return self.fake_store.get(key)

        self.redis_mock = MagicMock()
        self.redis_mock.set.side_effect = fake_set
        self.redis_mock.get.side_effect = fake_get

        patcher = patch("user_devices.mqtt.cache._redis", self.redis_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_put_and_get_round_trip(self):
        from user_devices.mqtt.cache import put_raw, get_raw

        put_raw(42, {0x280: 2301, 0x281: 15}, ts=time.time())
        result = get_raw(42)

        self.assertEqual(result, {0x280: 2301, 0x281: 15})

    def test_stale_entry_returns_none(self):
        from user_devices.mqtt.cache import put_raw, get_raw

        # Simula un'entry di 10 minuti fa (oltre la freshness default di 5 min)
        put_raw(42, {0x0: 1}, ts=time.time() - 600)
        result = get_raw(42)

        self.assertIsNone(result)

    def test_missing_entry_returns_none(self):
        from user_devices.mqtt.cache import get_raw

        self.assertIsNone(get_raw(9999))
