"""Atomic RedisState lock refresh under TTL overrun (#151).

`RedisState.set_state` refreshes the single value+lock key. The token
gate (#139) already skips the refresh when the key *already* carries a
successor's token at read time. The residual #151 window is a takeover
landing **strictly between** the read and the write: a stale holder that
still saw its own token when it read could, with a plain read-write,
re-plant that token over the successor's lock — and its later `unlock()`
would then compare-and-delete the successor's live lock.

On django-redis the refresh is a single server-side compare-and-set: the
write applies only if the key still holds exactly the bytes the token
decision was based on. This test drives a takeover into that exact window
and asserts the successor's lock is left intact. It needs a real Redis
(the CAS runs a Lua `EVAL`); LocMemCache / the single-process fake cannot
exhibit the race and take the non-atomic fallback.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import tag

from django_logic.state import RedisState

from tests.models import Invoice
from tests.stability.base import StabilityTestCase, requires_real_redis


class _TakeoverBetweenReadAndWrite:
    """Wraps the real connection so the first ``get`` — set_state's read —
    fires a one-shot takeover before returning the pre-takeover bytes,
    reproducing a successor acquiring the lock in the read→write window."""

    def __init__(self, real, takeover):
        self._real = real
        self._takeover = takeover
        self._fired = False

    def get(self, key):
        raw = self._real.get(key)
        if not self._fired:
            self._fired = True
            self._takeover()
        return raw

    def eval(self, *args, **kwargs):
        return self._real.eval(*args, **kwargs)


@tag('stability')
@requires_real_redis
class RedisStateAtomicRefreshTests(StabilityTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.invoice = Invoice.objects.create(status='draft')

    def _state(self):
        return RedisState(Invoice.objects.get(pk=self.invoice.pk), 'status')

    def test_takeover_between_read_and_write_cannot_replant_token(self):
        from django_redis import get_redis_connection

        t1 = self._state()
        self.assertTrue(t1.lock())

        t2 = self._state()

        def takeover():
            # T1's TTL expires and T2 acquires the lock with a fresh token,
            # all after T1's set_state has read the old value.
            cache.delete(t1._get_hash())
            self.assertTrue(t2.lock())

        wrapper = _TakeoverBetweenReadAndWrite(
            get_redis_connection('default'), takeover)

        with patch.object(RedisState, '_redis_conn', staticmethod(lambda: wrapper)):
            t1.set_state('late_write')

        # The CAS saw the key change under it and skipped the write, so
        # T2's token — not T1's — still owns the key.
        stored = cache.get(t2._get_hash())
        self.assertEqual(t2._stored_token(stored), t2._lock_token)
        self.assertNotEqual(t2._stored_token(stored), t1._lock_token)

        # The DB write still landed (the state guard arbitrates the value).
        self.assertEqual(
            Invoice.objects.get(pk=self.invoice.pk).status, 'late_write')

        # T1's late unlock therefore cannot release T2's lock.
        t1.unlock()
        self.assertTrue(t2.is_locked())

        t2.unlock()
        self.assertFalse(t2.is_locked())
