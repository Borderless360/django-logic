"""Per-transition ``lock_timeout`` (synchronous execution path).

The state lock is the liveness signal ``recover_stranded_states`` relies
on: a sync run that outlives its lock TTL becomes indistinguishable from
a stranded one. Transitions whose side-effects legitimately run long
(report generation, large exports) declare their own
``Transition(..., lock_timeout=...)`` instead of inflating the global
``LOCK_TIMEOUT`` for everyone.
"""
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from django_logic.process import Process, ProcessManager
from django_logic.state import RedisState, State
from django_logic.transition import Transition
from tests.models import Invoice


class _SlowReportProcess(Process):
    process_name = 'slow_report_process'
    transitions = [
        Transition('generate', sources=['draft'], target='done',
                   in_progress_state='generating',
                   failed_state='failed',
                   lock_timeout=4 * 3600),
        Transition('quick', sources=['draft'], target='done'),
    ]


class StateLockTimeoutTests(TestCase):
    def setUp(self):
        super().setUp()
        self.invoice = Invoice.objects.create(status='draft')

    def test_lock_passes_custom_timeout_to_the_cache(self):
        state = State(self.invoice, 'status')
        with mock.patch('django_logic.state.cache') as cache:
            cache.add.return_value = True
            self.assertTrue(state.lock(123))
        self.assertEqual(cache.add.call_args[0][2], 123)

    def test_lock_defaults_to_the_global_timeout(self):
        state = State(self.invoice, 'status')
        with mock.patch('django_logic.state.cache') as cache:
            cache.add.return_value = True
            state.lock()
        self.assertEqual(cache.add.call_args[0][2], 7200)

    def test_redis_state_set_state_refresh_keeps_the_custom_ttl(self):
        """RedisState.set_state refreshes the key TTL while locked; it
        must reuse the TTL the lock was taken with, not silently shorten
        a custom one back to the global default mid-run."""
        state = RedisState(self.invoice, 'status')
        with mock.patch('django_logic.state.cache') as cache:
            cache.set.return_value = True
            state.lock(900)
            self.assertEqual(cache.set.call_args[0][2], 900)
            state.set_state('generating')
        self.assertEqual(cache.set.call_args[0][2], 900)


class TransitionLockTimeoutTests(TestCase):
    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, _SlowReportProcess, state_field='status')

    def tearDown(self):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class is not _SlowReportProcess
        ]
        if 'slow_report_process' in vars(Invoice):
            delattr(Invoice, 'slow_report_process')
        super().tearDown()

    def _run_and_capture_lock_ttl(self, action):
        invoice = Invoice.objects.create(status='draft')
        ttls = []
        original_lock = State.lock

        def recording_lock(state_self, timeout=None):
            ttls.append(timeout)
            return original_lock(state_self, timeout)

        with mock.patch.object(State, 'lock', recording_lock):
            getattr(invoice.slow_report_process, action)()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'done')
        return ttls

    def test_sync_transition_locks_with_its_declared_timeout(self):
        self.assertEqual(self._run_and_capture_lock_ttl('generate'),
                         [4 * 3600])

    def test_transition_without_lock_timeout_uses_the_global(self):
        self.assertEqual(self._run_and_capture_lock_ttl('quick'), [None])

    def test_invalid_lock_timeout_is_rejected_at_declaration(self):
        for bad in (0, -5, 'long', True):
            with self.assertRaises(ImproperlyConfigured):
                Transition('x', sources=['a'], target='b', lock_timeout=bad)
