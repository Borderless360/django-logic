"""DJANGO_LOGIC['DEFER_UNLOCK_UNTIL_COMMIT'] (#141).

Inside an outer ``transaction.atomic()`` a synchronous transition's state
write is invisible to other connections until commit, while the cache
lock is not transactional. By default the lock is released as soon as the
transition completes (historical behavior — documented stale-read window,
see the stability suite's two-connection test). With the setting on, the
unlock is deferred to ``transaction.on_commit`` so exclusion covers the
whole invisible span.
"""
from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings

from django_logic.exceptions import TransitionNotAllowed
from django_logic.process import Process
from django_logic.state import State
from django_logic.transition import Transition
from tests.models import Invoice


def _boom(instance, **kwargs):
    raise ValueError('side effect failed')


class _DeferProcess(Process):
    process_name = 'defer_process'
    transitions = [
        Transition('approve', sources=['draft'], target='approved'),
        Transition('fulfill', sources=['approved'], target='fulfilled'),
        Transition('explode', sources=['draft'], target='done',
                   failed_state='failed', side_effects=[_boom]),
    ]


_DEFER_ON = {
    'BACKGROUND_EXECUTION': 'sync',
    'DEFER_UNLOCK_UNTIL_COMMIT': True,
}


class DefaultImmediateUnlockTests(TestCase):
    """Default (setting off): unlock is immediate even inside an atomic
    block — the historical contract stays untouched."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_unlock_is_immediate_inside_atomic(self):
        invoice = Invoice.objects.create(status='draft')
        state = State(invoice, 'status', process_name='defer_process')

        # TestCase wraps the test in an atomic block already; make the
        # nesting explicit anyway.
        with transaction.atomic():
            _DeferProcess(field_name='status', instance=invoice).approve()
            self.assertFalse(state.is_locked())


@override_settings(DJANGO_LOGIC=_DEFER_ON)
class DeferUnlockUntilCommitTests(TestCase):
    def setUp(self):
        super().setUp()
        # Deferred unlocks hang off the test's outer atomic, which rolls
        # back instead of committing — clear leftover lock keys so tests
        # stay independent (rolled-back pks get reused on sqlite).
        cache.clear()
        self.invoice = Invoice.objects.create(status='draft')
        self.state = State(self.invoice, 'status', process_name='defer_process')

    def _process(self, instance=None):
        return _DeferProcess(field_name='status', instance=instance or self.invoice)

    def test_unlock_deferred_to_commit_on_success(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._process().approve()
            # Still locked while the surrounding transaction is open.
            self.assertTrue(self.state.is_locked())
            self.assertEqual(self.invoice.status, 'approved')
        # captureOnCommitCallbacks ran the deferred hooks (= commit).
        self.assertFalse(self.state.is_locked())

    def test_second_transition_rejected_until_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._process().approve()
            with self.assertRaises(TransitionNotAllowed):
                self._process().fulfill()
        self.assertFalse(self.state.is_locked())
        # After "commit" the follow-up goes through.
        self._process().fulfill()
        self.assertEqual(self.invoice.status, 'fulfilled')

    def test_unlock_deferred_on_failure_path(self):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValueError):
                self._process().explode()
            self.assertTrue(self.state.is_locked())
            self.invoice.refresh_from_db()
            self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.state.is_locked())

    def test_revalidation_failure_still_unlocks_immediately(self):
        # A transition rejected under the lock (persisted state no longer
        # a source) wrote nothing — its unlock must stay immediate, or a
        # racing loser would leave the instance locked until commit.
        self.invoice.status = 'draft'
        other = Invoice.objects.get(pk=self.invoice.pk)
        other.status = 'approved'
        other.save(update_fields=['status'])
        with self.assertRaises(TransitionNotAllowed):
            self._process().approve()
        self.assertFalse(self.state.is_locked())


@override_settings(DJANGO_LOGIC=_DEFER_ON)
class DeferUnlockAutocommitTests(TransactionTestCase):
    """With the setting on but no surrounding transaction, the unlock is
    immediate — deferral only engages inside an atomic block."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_unlock_immediate_outside_atomic(self):
        invoice = Invoice.objects.create(status='draft')
        state = State(invoice, 'status', process_name='defer_process')

        _DeferProcess(field_name='status', instance=invoice).approve()

        self.assertFalse(state.is_locked())
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'approved')

    def test_rollback_leaves_bounded_lock(self):
        """Documented trade-off: on rollback the on_commit hook never
        fires, so the lock stays held until its TTL expires (bounded
        lockout, same failure mode as a crashed process)."""
        invoice = Invoice.objects.create(status='draft')
        state = State(invoice, 'status', process_name='defer_process')

        try:
            with transaction.atomic():
                _DeferProcess(field_name='status', instance=invoice).approve()
                raise RuntimeError('outer transaction failure')
        except RuntimeError:
            pass

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'draft')  # state write rolled back
        self.assertTrue(state.is_locked())  # lock waits out its TTL

        # A tokenless force-release (manual repair path) clears it.
        State(invoice, 'status', process_name='defer_process').unlock()
        self.assertFalse(state.is_locked())
