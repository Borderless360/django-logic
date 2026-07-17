"""kwargs serialization: typed round-trip, loud request drop, user_id swap."""
import json
from datetime import date, datetime, time, timezone as tz
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from django_logic.background.serializers import (
    decode_value, deserialize_kwargs, restore_user, serialize_kwargs,
)


def _roundtrip(kwargs):
    persisted = serialize_kwargs(kwargs)
    # The persisted form must be valid JSON as-is (what the DB row stores).
    return json.loads(json.dumps(persisted))


class SerializeKwargsTests(SimpleTestCase):
    def test_request_is_dropped_with_a_warning(self):
        with self.assertLogs('django-logic.transition', level='WARNING') as logs:
            out = serialize_kwargs({'request': Mock(), 'x': 1})
        self.assertNotIn('request', out)
        self.assertEqual(out['x'], 1)
        self.assertIn("'request' dropped", logs.output[0])

    @override_settings(DJANGO_LOGIC={'STRICT_KWARGS_SERIALIZATION': True})
    def test_request_raises_under_strict_setting(self):
        with self.assertRaisesMessage(TypeError, "'request' dropped"):
            serialize_kwargs({'request': Mock(), 'x': 1})

    @override_settings(DJANGO_LOGIC={'STRICT_KWARGS_SERIALIZATION': True})
    def test_strict_setting_leaves_clean_kwargs_alone(self):
        self.assertEqual(serialize_kwargs({'x': 1}), {'x': 1})

    def test_user_replaced_with_user_id(self):
        # serialize reads .pk (matching the phase-2 get(pk=...) restore and
        # custom-PK user models), not .id.
        user = Mock()
        user.pk = 42
        out = serialize_kwargs({'user': user})
        self.assertNotIn('user', out)
        self.assertEqual(out['user_id'], 42)

    def test_typed_values_round_trip_identically(self):
        aware = datetime(2024, 1, 2, 3, 4, 5, tzinfo=tz.utc)
        naive = datetime(2026, 6, 4, 12, 30, 0)
        original = {
            'aware': aware,
            'naive': naive,
            'day': date(2024, 1, 2),
            'at': time(23, 59, 1),
            'amount': Decimal('19.99'),
            'some_id': UUID('12345678-1234-5678-1234-567812345678'),
            'pair': (1, 'two'),
            'tags': {'a', 'b'},
            'frozen': frozenset({3}),
        }
        restored = decode_value(_roundtrip(dict(original)))
        self.assertEqual(restored, original)
        for key in original:
            self.assertIs(type(restored[key]), type(original[key]), key)

    def test_nested_containers_round_trip(self):
        original = {
            'list': [UUID(int=1), date(2024, 1, 1), (Decimal('1'), {'x'})],
            'dict': {'nested': {'deeper': datetime(2024, 5, 6, tzinfo=tz.utc)}},
        }
        self.assertEqual(decode_value(_roundtrip(dict(original))), original)

    def test_caller_dict_containing_the_tag_key_is_escaped(self):
        original = {'payload': {'__dl_type__': 'not-ours', 'value': 1}}
        self.assertEqual(decode_value(_roundtrip(dict(original))), original)

    def test_legacy_untagged_rows_pass_through(self):
        # A row written before the typed encoding: plain strings stay strings.
        legacy = {'when': '2024-01-02T03:04:05+00:00', 'some_id': 'abc', 'n': 1}
        self.assertEqual(decode_value(dict(legacy)), legacy)

    def test_unknown_tag_passes_through_with_a_warning(self):
        row = {'v': {'__dl_type__': 'from_the_future', 'value': 'x'}}
        with self.assertLogs('django-logic.transition', level='WARNING'):
            self.assertEqual(decode_value(dict(row)), row)

    def test_tr_ids_stringified_when_uuid(self):
        tr_id = UUID(int=99)
        out = serialize_kwargs({
            'tr_id': tr_id, 'root_id': tr_id, 'parent_id': tr_id,
        })
        self.assertEqual(out['tr_id'], str(tr_id))
        self.assertEqual(out['root_id'], str(tr_id))
        self.assertEqual(out['parent_id'], str(tr_id))

    def test_unserializable_raises_at_phase1(self):
        class Unserializable:
            pass

        with self.assertRaises(TypeError):
            serialize_kwargs({'blob': Unserializable()})

    def test_context_kwarg_stripped(self):
        out = serialize_kwargs({'context': {'x': 1}, 'keep': 2})
        self.assertNotIn('context', out)
        self.assertEqual(out['keep'], 2)

    def test_round_trip_through_json(self):
        out = serialize_kwargs({'a': 1, 'b': 'x', 'c': None})
        # Must be valid JSON as-is.
        self.assertEqual(json.loads(json.dumps(out)), out)


class DeserializeKwargsTests(SimpleTestCase):
    def test_none_and_empty_rows(self):
        self.assertEqual(deserialize_kwargs(None), {})
        self.assertEqual(deserialize_kwargs({}), {})


class RestoreUserTests(SimpleTestCase):
    def test_no_user_id_is_noop(self):
        kwargs = {'other': 1}
        restore_user(kwargs)
        self.assertEqual(kwargs, {'other': 1})
