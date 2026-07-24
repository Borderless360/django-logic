"""DJANGO_LOGIC['DEFER_UNLOCK_UNTIL_COMMIT'] (#141).

Inside an outer ``transaction.atomic()`` a synchronous transition's state
write is invisible to other connections until commit, while the cache
lock is not transactional. By default the lock is released as soon as the
transition completes (historical behavior — documented stale-read window,
see the stability suite's two-connection test). With the setting on, the
unlock is deferred to ``transaction.on_commit`` so exclusion covers the
whole invisible span.
"""
from unittest import mock

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


_OTHER = {}


def _drive_other_then_fail(instance, **kwargs):
    """A hook that drives ANOTHER instance's transition (registering that
    instance's deferred unlock inside this hook's savepoint) and then
    blows up — the savepoint rollback used to discard the on_commit
    unlock and leak the other instance's lock until TTL."""
    other = Invoice.objects.get(pk=_OTHER['pk'])
    _DeferProcess(field_name='status', instance=other).approve()
    raise ValueError('hook failed after driving another instance')


def _drive_two_others_then_fail(instance, **kwargs):
    for pk in _OTHER['pks']:
        other = Invoice.objects.get(pk=pk)
        _DeferProcess(field_name='status', instance=other).approve()
    raise ValueError('hook failed after driving two other instances')


class _DeferProcess(Process):
    process_name = 'defer_process'
    transitions = [
        Transition('approve', sources=['draft'], target='approved'),
        Transition('fulfill', sources=['approved'], target='fulfilled'),
        Transition('explode', sources=['draft'], target='done',
                   failed_state='failed', side_effects=[_boom]),
        # Fails without writing ANY state under the lock: no
        # in_progress_state, no failed_state.
        Transition('explode_bare', sources=['draft'], target='done',
                   side_effects=[_boom]),
        # Fails after writing in_progress_state (but no failed_state).
        Transition('explode_in_progress', sources=['draft'], target='done',
                   in_progress_state='working', side_effects=[_boom]),
        # Succeeds, then a callback drives another instance and fails.
        Transition('chain_hook', sources=['draft'], target='approved',
                   callbacks=[_drive_other_then_fail]),
        Transition('chain_hook_two', sources=['draft'], target='approved',
                   callbacks=[_drive_two_others_then_fail]),
        # Fails; a failure side-effect drives another instance and fails.
        Transition('cleanup_chain', sources=['draft'], target='done',
                   failed_state='failed', side_effects=[_boom],
                   failure_side_effects=[_drive_other_then_fail]),
        # Plain in-progress transition for the failed-target-write tests.
        Transition('finish_ip', sources=['draft'], target='done_ip',
                   in_progress_state='ip_working'),
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

    def test_failure_without_any_state_write_unlocks_immediately(self):
        """No in_progress_state, no failed_state: nothing was written
        under the lock, so there is no invisible span to protect —
        deferring would only leak the lock until TTL on rollback."""
        with self.assertRaises(ValueError):
            self._process().explode_bare()
        self.assertFalse(self.state.is_locked())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')

    def test_failure_after_in_progress_write_defers(self):
        """The in_progress_state written in change_state IS a state write
        under this lock — its visibility span is protected like any
        other."""
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValueError):
                self._process().explode_in_progress()
            self.assertTrue(self.state.is_locked())
        self.assertFalse(self.state.is_locked())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'working')

    def test_hook_savepoint_rollback_releases_inner_deferred_unlock(self):
        """A failing callback's savepoint rollback discards the on_commit
        unlocks registered inside it — the engine must release those
        locks itself, or the other instance stays locked until TTL while
        its state write is already rolled back."""
        other = Invoice.objects.create(status='draft')
        _OTHER['pk'] = other.pk
        other_state = State(other, 'status', process_name='defer_process')

        with self.captureOnCommitCallbacks(execute=True):
            self._process().chain_hook()
            # The callback's write to the other instance rolled back with
            # its savepoint — and its lock was released, not leaked.
            self.assertFalse(other_state.is_locked())
            self.assertEqual(Invoice.objects.get(pk=other.pk).status, 'draft')
            # The main transition's own deferral is unaffected.
            self.assertTrue(self.state.is_locked())
        self.assertFalse(self.state.is_locked())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'approved')

    def test_savepoint_cleanup_survives_a_failing_unlock(self):
        """One unlock raising (cache blip) must neither skip the sibling
        releases nor replace the hook's original exception — the missed
        release degrades to the documented TTL-bounded leak."""
        first = Invoice.objects.create(status='draft')
        second = Invoice.objects.create(status='draft')
        _OTHER['pks'] = [first.pk, second.pk]
        first_state = State(first, 'status', process_name='defer_process')
        second_state = State(second, 'status', process_name='defer_process')

        original_unlock = State.unlock

        def blipping_unlock(state_self):
            if state_self.instance.pk == first.pk:
                raise ConnectionError('cache blip')
            return original_unlock(state_self)

        with mock.patch.object(State, 'unlock', blipping_unlock):
            with self.captureOnCommitCallbacks(execute=False):
                # The hook's ValueError is swallowed (best-effort) — the
                # cleanup's ConnectionError must not replace it either.
                self._process().chain_hook_two()

        # First lock leaked (TTL-bounded, logged); the SECOND was still
        # released despite the earlier blip.
        self.assertTrue(first_state.is_locked())
        self.assertFalse(second_state.is_locked())

        # Manual cleanup for the leaked key.
        State(first, 'status', process_name='defer_process').unlock()

    def test_failure_side_effect_savepoint_rollback_releases_inner_deferred_unlock(self):
        other = Invoice.objects.create(status='draft')
        _OTHER['pk'] = other.pk
        other_state = State(other, 'status', process_name='defer_process')

        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValueError):
                self._process().cleanup_chain()
            self.assertFalse(other_state.is_locked())
            self.assertEqual(Invoice.objects.get(pk=other.pk).status, 'draft')
            # Own failed_state write was real: deferral holds.
            self.assertTrue(self.state.is_locked())
        self.assertFalse(self.state.is_locked())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'failed')

    def test_failed_target_write_defers_when_in_progress_was_written(self):
        """A failed target write after a real in_progress write keeps the
        deferral — an immediate release would reopen the
        unlock-before-commit window for the uncommitted in_progress."""
        original = State.set_state

        def failing_target(state_self, value):
            if value == 'done_ip':
                raise RuntimeError('target write failed')
            return original(state_self, value)

        with mock.patch.object(State, 'set_state', failing_target):
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(RuntimeError):
                    self._process().finish_ip()
                self.assertTrue(self.state.is_locked())
        self.assertFalse(self.state.is_locked())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'ip_working')

    def test_failed_target_write_without_prior_write_unlocks_immediately(self):
        original = State.set_state

        def failing_target(state_self, value):
            if value == 'approved':
                raise RuntimeError('target write failed')
            return original(state_self, value)

        with mock.patch.object(State, 'set_state', failing_target):
            with self.assertRaises(RuntimeError):
                self._process().approve()
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
