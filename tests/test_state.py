from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from django_logic.state import State, RedisState
from tests.models import Invoice


class FakeRedisCache:
    """In-memory cache that supports nx=True, mimicking django-redis."""

    def __init__(self):
        self._store = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, timeout=None, nx=False):
        if nx and key in self._store:
            return False
        self._store[key] = value
        return True

    def delete(self, key):
        self._store.pop(key, None)

    def clear(self):
        self._store.clear()


class StateTestCase(TestCase):
    def setUp(self) -> None:
        self.state = State(Invoice.objects.create(status='draft'), 'status')

    def test_hash_remains_the_same(self):
        self.assertEqual(self.state._get_hash(), self.state._get_hash())

    def test_get_db_state(self):
        self.assertEqual(self.state.get_db_state(), 'draft')

    def test_lock(self):
        self.assertFalse(self.state.is_locked())
        self.state.lock()
        self.assertTrue(self.state.is_locked())

        # nothing should happen
        self.state.lock()
        self.assertTrue(self.state.is_locked())

        self.state.unlock()
        self.assertFalse(self.state.is_locked())

    def test_set_state(self):
        self.state.set_state('void')
        self.assertEqual(self.state.instance.status, 'void')
        # make sure it was saved to db
        self.state.instance.refresh_from_db()
        self.assertEqual(self.state.instance.status, 'void')


@patch('django_logic.state.cache', new_callable=FakeRedisCache)
class RedisStateTestCase(TestCase):
    def setUp(self) -> None:
        self.instance = Invoice.objects.create(status='draft')
        self.state = RedisState(self.instance, 'status')

    def test_get_state_returns_instance_attr_when_unlocked(self, mock_cache):
        self.assertEqual(self.state.get_state(), 'draft')

    def test_lock_stores_current_state(self, mock_cache):
        self.state.lock()
        self.assertEqual(mock_cache.get(self.state._get_hash()), 'draft')

    def test_lock_is_atomic(self, mock_cache):
        self.assertTrue(self.state.lock())
        other = RedisState(Invoice.objects.get(pk=self.instance.pk), 'status')
        self.assertFalse(other.lock())

    def test_is_locked_after_lock(self, mock_cache):
        self.assertFalse(self.state.is_locked())
        self.state.lock()
        self.assertTrue(self.state.is_locked())

    def test_set_state_updates_cache_and_db(self, mock_cache):
        self.state.lock()
        self.state.set_state('in_progress')

        self.assertEqual(mock_cache.get(self.state._get_hash()), 'in_progress')
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, 'in_progress')

    def test_get_state_reads_from_cache_when_locked(self, mock_cache):
        self.state.lock()
        self.state.set_state('in_progress')
        self.assertEqual(self.state.get_state(), 'in_progress')

    def test_get_state_visible_to_other_processes(self, mock_cache):
        self.state.lock()
        self.state.set_state('in_progress')

        other = RedisState(Invoice.objects.get(pk=self.instance.pk), 'status')
        self.assertEqual(other.get_state(), 'in_progress')

    def test_get_db_state_prefers_cache(self, mock_cache):
        self.state.lock()
        self.state.set_state('in_progress')
        self.assertEqual(self.state.get_db_state(), 'in_progress')

    def test_get_db_state_falls_back_to_db(self, mock_cache):
        self.assertEqual(self.state.get_db_state(), 'draft')

    def test_unlock_removes_key(self, mock_cache):
        self.state.lock()
        self.state.set_state('completed')
        self.state.unlock()

        self.assertIsNone(mock_cache.get(self.state._get_hash()))
        self.assertFalse(self.state.is_locked())

    def test_get_state_falls_back_after_unlock(self, mock_cache):
        self.state.lock()
        self.state.set_state('completed')
        self.state.unlock()

        self.assertEqual(self.state.get_state(), 'completed')

    def test_full_transition_lifecycle(self, mock_cache):
        """lock → in_progress → target → unlock"""
        self.assertEqual(self.state.get_state(), 'draft')

        self.assertTrue(self.state.lock())
        self.assertEqual(self.state.get_state(), 'draft')

        self.state.set_state('in_progress')
        self.assertEqual(self.state.get_state(), 'in_progress')

        other = RedisState(Invoice.objects.get(pk=self.instance.pk), 'status')
        self.assertEqual(other.get_state(), 'in_progress')
        self.assertTrue(other.is_locked())
        self.assertFalse(other.lock())

        self.state.set_state('completed')
        self.assertEqual(other.get_state(), 'completed')

        self.state.unlock()
        self.assertFalse(self.state.is_locked())
        self.assertEqual(self.state.get_state(), 'completed')
