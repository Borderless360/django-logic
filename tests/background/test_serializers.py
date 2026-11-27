"""Plain JSON arguments and user restoration."""
import json
from datetime import date, datetime, time, timezone as tz
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

from django.test import SimpleTestCase

from django_logic.background.serializers import (
    KwargsSerializationError, deserialize_kwargs, restore_user, serialize_kwargs,
)


class SerializeKwargsTests(SimpleTestCase):
    def test_clean_kwargs_pass_through(self):
        self.assertEqual(serialize_kwargs({'x': 1}), {'x': 1})

    def test_user_replaced_with_user_id(self):
        # serialize reads .pk (matching the get(pk=...) restore on the worker and
        # custom-PK user models), not .id.
        user = Mock()
        user.pk = 42
        out = serialize_kwargs({'user': user})
        self.assertNotIn('user', out)
        self.assertEqual(out['user_id'], 42)

    def test_non_json_arguments_name_the_field(self):
        for value in (datetime.now(tz.utc), date.today(), time(12), Decimal('1'), UUID(int=1),
                      (1, 2), {1}, frozenset({1})):
            with self.subTest(value=value), self.assertRaisesRegex(TypeError, 'items'):
                serialize_kwargs({'items': [{'value': value}]})

    def test_legacy_tag_refused_before_hook(self):
        with self.assertRaisesMessage(KwargsSerializationError, 'legacy typed job argument'):
            deserialize_kwargs({'items': [{'__dl_type__': 'date', 'value': '2026-01-01'}]})

    def test_tr_ids_stringified_when_uuid(self):
        tr_id = UUID(int=99)
        out = serialize_kwargs({
            'tr_id': tr_id, 'root_id': tr_id, 'parent_id': tr_id,
        })
        self.assertEqual(out['tr_id'], str(tr_id))
        self.assertEqual(out['root_id'], str(tr_id))
        self.assertEqual(out['parent_id'], str(tr_id))

    def test_unserializable_raises_at_enqueue(self):
        class Unserializable:
            pass

        with self.assertRaises(TypeError):
            serialize_kwargs({'blob': Unserializable()})

    def test_non_finite_floats_rejected(self):
        # They pass Python's json.dumps (non-standard NaN/Infinity tokens)
        # but are not valid JSON, so the failure would otherwise surface
        # backend-dependently at the row write.
        for value in (float('nan'), float('inf'), float('-inf')):
            with self.subTest(value=value):
                with self.assertRaisesMessage(
                        TypeError, 'kwargs are not valid JSON'):
                    serialize_kwargs({'rate': value})

    def test_nested_non_finite_float_rejected(self):
        with self.assertRaisesMessage(TypeError, 'kwargs are not valid JSON'):
            serialize_kwargs({'stats': {'values': [1.0, float('nan')]}})

    def test_non_finite_float_in_a_dict_key_rejected(self):
        # A float key is refused as a non-string key before the JSON
        # round-trip could see that it is also non-finite.
        with self.assertRaisesMessage(TypeError, 'non-string dict keys'):
            serialize_kwargs({'stats': {float('nan'): 1}})

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
        # The enqueue dispatcher wraps generic TypeError in
        # ImproperlyConfigured ("not JSON-serializable") — the strict-mode
        # rejection must stay distinguishable so it propagates verbatim.
        with self.assertRaises(KwargsSerializationError) as caught:
            serialize_kwargs({'user_id': 42})
        self.assertIn('user_id', str(caught.exception))

    def test_non_string_dict_keys_are_refused(self):
        # JSON stringifies int keys silently ({1: 'a'} -> {"1": "a"}), which
        # breaks the type-faithful contract — enqueue refuses instead.
        with self.assertRaisesMessage(TypeError, 'non-string dict keys') as ctx:
            serialize_kwargs({'m': {1: 'a'}})
        self.assertIn("'m'", str(ctx.exception))

    def test_non_string_dict_keys_nested_in_containers_are_refused(self):
        with self.assertRaisesMessage(TypeError, 'non-string dict keys'):
            serialize_kwargs({'items': [{'deep': {2: 'b'}}]})


class DeserializeKwargsTests(SimpleTestCase):
    def test_none_and_empty_rows(self):
        self.assertEqual(deserialize_kwargs(None), {})
        self.assertEqual(deserialize_kwargs({}), {})


class RestoreUserTests(SimpleTestCase):
    def test_no_user_id_is_noop(self):
        kwargs = {'other': 1}
        restore_user(kwargs)
        self.assertEqual(kwargs, {'other': 1})
