"""Lock ownership tokens (#139).

State locks store a unique per-acquisition token, and ``unlock()`` is a
compare-and-delete: a holder whose lock TTL-expired cannot release a
successor's lock. The canonical hazard:

    T1 locks, exceeds its TTL (key expires) → T2 acquires the lock →
    T1 finishes late and calls unlock() → pre-#139 that deleted T2's
    lock, letting T3 enter concurrently with T2.

Covered for both supported lock implementations (``State`` on the generic
cache backend, ``RedisState`` on its single-key value+lock storage).
"""
from unittest.mock import patch

from django.core.cache import cache as django_cache
from django.test import TestCase

from django_logic.state import RedisState, State
from tests.models import Invoice
from tests.test_state import FakeRedisCache


class StateOwnershipTokenTests(TestCase):
    """Base State on the default (locmem) cache backend."""

    def setUp(self):
        super().setUp()
        django_cache.clear()
        self.invoice = Invoice.objects.create(status='draft')

    def _state(self):
        return State(Invoice.objects.get(pk=self.invoice.pk), 'status')

    def test_expired_holder_cannot_unlock_successor(self):
        t1 = self._state()
        self.assertTrue(t1.lock())

        # Simulate T1's TTL expiry: the key vanishes without T1 unlocking.
        django_cache.delete(t1._get_hash())

        t2 = self._state()
        self.assertTrue(t2.lock())

        # T1 finishes late. Its token no longer matches — T2's lock survives.
        t1.unlock()
        self.assertTrue(t2.is_locked())

        # T3 stays excluded while T2 holds the lock.
        t3 = self._state()
        self.assertFalse(t3.lock())

        # T2's own unlock still works.
        t2.unlock()
        self.assertFalse(t2.is_locked())

    def test_unique_token_per_acquisition(self):
        t1 = self._state()
        self.assertTrue(t1.lock())
        token1 = t1._lock_token
        t1.unlock()

        t2 = self._state()
        self.assertTrue(t2.lock())
        self.assertNotEqual(token1, t2._lock_token)
        t2.unlock()

    def test_failed_acquisition_does_not_clobber_own_token(self):
        t1 = self._state()
        self.assertTrue(t1.lock())
        token = t1._lock_token

        # A second lock() on the same object fails (already locked) and
        # must not overwrite the token of the acquisition it still owns.
        self.assertFalse(t1.lock())
        self.assertEqual(t1._lock_token, token)

        t1.unlock()
        self.assertFalse(t1.is_locked())

    def test_unlock_without_token_force_releases(self):
        # A State object that never locked keeps the historical
        # force-release behavior (manual repair path).
        t1 = self._state()
        self.assertTrue(t1.lock())

        repair = self._state()
        repair.unlock()
        self.assertFalse(t1.is_locked())

    def test_double_unlock_is_harmless(self):
        t1 = self._state()
        self.assertTrue(t1.lock())
        t1.unlock()

        t2 = self._state()
        self.assertTrue(t2.lock())

        # T1's second unlock (e.g. an ownership-transfer path calling
        # through fail_transition's finally) must not steal T2's lock.
        t1.unlock()
        self.assertTrue(t2.is_locked())
        t2.unlock()


@patch('django_logic.state.cache', new_callable=FakeRedisCache)
class RedisStateOwnershipTokenTests(TestCase):
    """RedisState: the token rides inside the single value+lock key."""

    def setUp(self):
        super().setUp()
        self.invoice = Invoice.objects.create(status='draft')

    def _state(self):
        return RedisState(Invoice.objects.get(pk=self.invoice.pk), 'status')

    def test_expired_holder_cannot_unlock_successor(self, mock_cache):
        t1 = self._state()
        self.assertTrue(t1.lock())

        # TTL expiry: key vanishes without T1 unlocking.
        mock_cache.delete(t1._get_hash())

        t2 = self._state()
        self.assertTrue(t2.lock())

        t1.unlock()
        self.assertTrue(t2.is_locked())

        t3 = self._state()
        self.assertFalse(t3.lock())

        t2.unlock()
        self.assertFalse(t2.is_locked())

    def test_set_state_preserves_holder_token(self, mock_cache):
        t1 = self._state()
        self.assertTrue(t1.lock())

        # State progression while locked must not lose ownership.
        t1.set_state('in_progress')
        t1.set_state('completed')
        t1.unlock()
        self.assertFalse(t1.is_locked())

    def test_non_holder_state_write_preserves_holder_token(self, mock_cache):
        t1 = self._state()
        self.assertTrue(t1.lock())

        # A different State object writing while the key is live (the
        # xx-refresh path) must keep T1's token so T1 can still unlock.
        other = self._state()
        other.set_state('in_progress')
        self.assertEqual(t1.get_state(), 'in_progress')

        t1.unlock()
        self.assertFalse(t1.is_locked())

    def test_legacy_raw_value_still_readable_and_not_cad_deleted(self, mock_cache):
        t1 = self._state()
        # A key written by a pre-token version stores the raw state value.
        mock_cache.set(t1._get_hash(), 'in_progress')

        self.assertTrue(t1.is_locked())
        self.assertEqual(t1.get_state(), 'in_progress')

        # A token-holding non-owner cannot delete it...
        t2 = self._state()
        t2._lock_token = 'not-the-owner'
        t2.unlock()
        self.assertTrue(t1.is_locked())

        # ...but a tokenless force-release still can.
        self._state().unlock()
        self.assertFalse(t1.is_locked())

    def test_none_state_round_trip_keeps_sentinel_semantics(self, mock_cache):
        # lock() snapshots the in-memory value; None must survive the
        # sentinel wrapping (is_locked() works, get_state() returns None).
        self.invoice.status = None
        t1 = RedisState(self.invoice, 'status')
        self.assertTrue(t1.lock())
        self.assertTrue(t1.is_locked())
        self.assertIsNone(t1.get_state())
        t1.unlock()
        self.assertFalse(t1.is_locked())
