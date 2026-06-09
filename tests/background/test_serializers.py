"""kwargs serialization: JSON-safe values, user_id swap, round-trip."""
import json
from datetime import date, datetime, timezone as tz
from unittest.mock import Mock
from uuid import UUID

from django.test import SimpleTestCase

from django_logic.background.serializers import restore_user, serialize_kwargs


class SerializeKwargsTests(SimpleTestCase):
    def test_request_is_dropped(self):
        out = serialize_kwargs({'request': Mock(), 'x': 1})
        self.assertNotIn('request', out)
        self.assertEqual(out['x'], 1)

    def test_user_replaced_with_user_id(self):
        # serialize reads .pk (matching the phase-2 get(pk=...) restore and
        # custom-PK user models), not .id.
        user = Mock()
        user.pk = 42
        out = serialize_kwargs({'user': user})
        self.assertNotIn('user', out)
        self.assertEqual(out['user_id'], 42)

    def test_uuid_stringified(self):
        u = UUID('12345678-1234-5678-1234-567812345678')
        out = serialize_kwargs({'some_id': u})
        self.assertEqual(out['some_id'], str(u))

    def test_datetime_isoformatted(self):
        dt = datetime(2024, 1, 2, 3, 4, 5, tzinfo=tz.utc)
        out = serialize_kwargs({'when': dt, 'day': date(2024, 1, 2)})
        self.assertEqual(out['when'], dt.isoformat())
        self.assertEqual(out['day'], '2024-01-02')

    def test_nested_containers_serialized(self):
        out = serialize_kwargs({
            'list': [UUID(int=1), date(2024, 1, 1)],
            'dict': {'nested': UUID(int=2)},
        })
        self.assertEqual(out['list'][0], str(UUID(int=1)))
        self.assertEqual(out['list'][1], '2024-01-01')
        self.assertEqual(out['dict']['nested'], str(UUID(int=2)))

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


class RestoreUserTests(SimpleTestCase):
    def test_no_user_id_is_noop(self):
        kwargs = {'other': 1}
        restore_user(kwargs)
        self.assertEqual(kwargs, {'other': 1})
