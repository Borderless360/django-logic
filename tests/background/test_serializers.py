"""kwargs serialization: typed round-trip, loud request drop, user_id swap."""
import json
from datetime import date, datetime, time, timezone as tz
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from django_logic.background.serializers import (
    decode_value, deserialize_kwargs, restore_user, serialize_kwargs,
    KwargsSerializationError,
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

    def test_malformed_scalar_payload_passes_through_with_a_warning(self):
        # A KNOWN tag whose payload no longer decodes (hand-edited row,
        # cross-version writer bug) must not crash phase 2 — same
        # passthrough contract as an unknown tag.
        row = {'when': {'__dl_type__': 'datetime', 'value': 'not-a-datetime'}}
        with self.assertLogs('django-logic.transition', level='WARNING') as logs:
            self.assertEqual(decode_value(dict(row)), row)
        self.assertIn('malformed payload', logs.output[0])
        self.assertIn("'datetime'", logs.output[0])

    def test_missing_payload_for_known_tag_passes_through(self):
        # Tagged dict without its 'value' key: the inner payload is None.
        row = {'when': {'__dl_type__': 'datetime'}}
        with self.assertLogs('django-logic.transition', level='WARNING'):
            self.assertEqual(decode_value(dict(row)), row)

    def test_malformed_container_payload_passes_through(self):
        for tag in ('dict', 'tuple', 'set', 'frozenset'):
            row = {'v': {'__dl_type__': tag, 'value': None}}
            with self.assertLogs('django-logic.transition', level='WARNING'):
                self.assertEqual(decode_value(dict(row)), row, tag)

    def test_well_formed_neighbours_of_a_malformed_payload_still_decode(self):
        row = {
            'bad': {'__dl_type__': 'uuid', 'value': 'not-a-uuid'},
            'good': {'__dl_type__': 'decimal', 'value': '1.5'},
        }
        with self.assertLogs('django-logic.transition', level='WARNING'):
            out = decode_value(dict(row))
        self.assertEqual(out['bad'], row['bad'])
        self.assertEqual(out['good'], Decimal('1.5'))

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

    def test_nan_rejected_naming_the_offending_value(self):
        # float('nan') passes Python's json.dumps (non-standard NaN token)
        # but is not valid JSON — the failure would otherwise surface
        # backend-dependently at the row write.
        with self.assertRaisesMessage(TypeError, "kwargs['rate']=nan"):
            serialize_kwargs({'rate': float('nan')})

    def test_infinity_rejected_naming_the_offending_value(self):
        with self.assertRaisesMessage(TypeError, "kwargs['rate']=inf"):
            serialize_kwargs({'rate': float('inf')})

    def test_negative_infinity_rejected(self):
        with self.assertRaisesMessage(TypeError, "kwargs['rate']=-inf"):
            serialize_kwargs({'rate': float('-inf')})

    def test_nested_non_finite_float_rejected_with_its_path(self):
        with self.assertRaisesMessage(
                TypeError, "kwargs['stats']['values'][]=nan"):
            serialize_kwargs({'stats': {'values': [1.0, float('nan')]}})

    def test_finite_floats_still_pass(self):
        self.assertEqual(serialize_kwargs({'rate': 1.5}), {'rate': 1.5})

    def test_context_kwarg_stripped(self):
        out = serialize_kwargs({'context': {'x': 1}, 'keep': 2})
        self.assertNotIn('context', out)
        self.assertEqual(out['keep'], 2)

    def test_round_trip_through_json(self):
        out = serialize_kwargs({'a': 1, 'b': 'x', 'c': None})
        # Must be valid JSON as-is.
        self.assertEqual(json.loads(json.dumps(out)), out)

    def test_strict_raise_is_a_distinct_typeerror_subclass(self):
        # The phase-1 dispatcher wraps generic TypeError in
        # ImproperlyConfigured ("not JSON-serializable") — the strict-mode
        # rejection must stay distinguishable so it propagates verbatim.
        with override_settings(DJANGO_LOGIC={'STRICT_KWARGS_SERIALIZATION': True}):
            with self.assertRaises(KwargsSerializationError):
                serialize_kwargs({'request': Mock()})

    def test_non_string_dict_keys_warn(self):
        # JSON stringifies int keys silently ({1: 'a'} -> {"1": "a"}), which
        # breaks the type-faithful contract — phase 1 must say so.
        with self.assertLogs('django-logic.transition', level='WARNING') as logs:
            out = serialize_kwargs({'m': {1: 'a'}})
        self.assertIn('non-string dict keys', logs.output[0])
        self.assertIn("'m'", logs.output[0])
        # The documented (lossy) persisted contract: keys become strings.
        self.assertEqual(json.loads(json.dumps(out)), {'m': {'1': 'a'}})

    def test_non_string_dict_keys_nested_in_containers_warn(self):
        with self.assertLogs('django-logic.transition', level='WARNING') as logs:
            serialize_kwargs({'items': [{'deep': {2: 'b'}}]})
        self.assertIn('non-string dict keys', logs.output[0])

    @override_settings(DJANGO_LOGIC={'STRICT_KWARGS_SERIALIZATION': True})
    def test_non_string_dict_keys_raise_under_strict_setting(self):
        with self.assertRaisesMessage(TypeError, 'non-string dict keys'):
            serialize_kwargs({'m': {1: 'a'}})


class DeserializeKwargsTests(SimpleTestCase):
    def test_none_and_empty_rows(self):
        self.assertEqual(deserialize_kwargs(None), {})
        self.assertEqual(deserialize_kwargs({}), {})


class RestoreUserTests(SimpleTestCase):
    def test_no_user_id_is_noop(self):
        kwargs = {'other': 1}
        restore_user(kwargs)
        self.assertEqual(kwargs, {'other': 1})
