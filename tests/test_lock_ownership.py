"""Lock ownership tokens (#139).

State locks store a unique per-acquisition token, and ``unlock()`` is a
compare-and-delete: a holder whose lock TTL-expired cannot release a
successor's lock. The canonical hazard:

    T1 locks, exceeds its TTL (key expires) → T2 acquires the lock →
    T1 finishes late and calls unlock() → pre-#139 that deleted T2's
    lock, letting T3 enter concurrently with T2.

Lock records use token-conditional database deletion.
"""

from datetime import timedelta
from django.db.models.functions import Now
from django_logic.models import StateLock
from django.test import TransactionTestCase

from django_logic.state import State
from tests.models import Invoice


class StateOwnershipTokenTests(TransactionTestCase):
    """State ownership remains safe when an expired lock is replaced."""

    def setUp(self):
        super().setUp()
        self.invoice = Invoice.objects.create(status='draft')

    def _state(self):
        return State(Invoice.objects.get(pk=self.invoice.pk), 'status')

    def test_expired_holder_cannot_unlock_successor(self):
        t1 = self._state()
        self.assertTrue(t1.lock())

        # Simulate T1's TTL expiry: the key vanishes without T1 unlocking.
        StateLock.objects.filter(key=t1._get_hash()).update(expires_at=Now() - timedelta(seconds=1))

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

    def test_unlock_without_token_preserves_the_holder(self):
        # A separate object cannot release a lock it never acquired.
        t1 = self._state()
        self.assertTrue(t1.lock())

        repair = self._state()
        repair.unlock()
        self.assertTrue(t1.is_locked())
        self.assertFalse(repair.lock())
        t1.unlock()
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


