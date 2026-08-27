"""The engine's own failure paths, pinned.

Every test here fails if its fix is reverted. Each assertion was checked by
mutation, because this suite has shipped assertions that passed against the
defect they were meant to catch.
"""
import logging
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage, db_safe_text
from django_logic.background.runner import (
    finalize_stuck_attempt,
    run_background_transition,
)
from django_logic.exceptions import (
    TransitionNotAllowed,
    TransitionTemporarilyUnavailable,
)
from tests.models import Invoice

_SYNC = {
    'BACKGROUND_EXECUTION': 'sync',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 0,
}


class _BindCleanup:
    """Clear the binding registry, which is shared by the whole process."""

    _bound = ()

    def tearDown(self):
        for process_class in self._bound:
            ProcessManager.unbind_model_process(Invoice, process_class)
        super().tearDown()


def _noop(instance, **kwargs):
    pass


def _boom(instance, **kwargs):
    raise ValueError('boom')


# --- The safety-net finalizers respect a manual state fix ------------------

#: The hooks record here instead of writing to the model. A model write would
#: roll back with the finalizer's savepoint and look the same as "never ran".
_HOOK_LOG: list = []


def _record_failure_callback(instance, **kwargs):
    _HOOK_LOG.append('fcb')


class SupersedeParityProcess(Process):
    process_name = 'supersede_parity_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='done',
            in_progress_state='sp_running', failed_state='sp_failed',
            side_effects=[_noop], timeout=60,
            failure_callbacks=[_record_failure_callback],
        ),
    ]


@override_settings(DJANGO_LOGIC=_SYNC)
class SupersedeParityTests(_BindCleanup, TestCase):
    """When an operator moves the instance by hand, the worker completes the
    row as superseded and runs no hooks. The safety-net finalizers must do the
    same, not only skip the ``failed_state`` write.
    """

    _bound = (SupersedeParityProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, SupersedeParityProcess, state_field='status')
        cache.clear()
        self.addCleanup(cache.clear)
        _HOOK_LOG.clear()

    def _row_at_max(self, status='sp_running', **overrides):
        invoice = Invoice.objects.create(status=status)
        fields = dict(
            app_label='tests', model_name='invoice',
            instance_id=str(invoice.pk),
            process_name='supersede_parity_proc', transition_name='go',
            queue_name='django_logic', errors_count=3, timeout_seconds=60,
        )
        fields.update(overrides)
        return invoice, TransitionMessage.objects.create(**fields)

    def test_detect_stuck_supersedes_and_runs_no_hooks(self):
        invoice, transition_message = self._row_at_max(status='fixed_by_hand')
        transition_message.record_error(ValueError('the original cause'))

        self.assertTrue(finalize_stuck_attempt(transition_message.pk))

        transition_message.refresh_from_db()
        invoice.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertTrue(
            transition_message.last_error_message.startswith('[superseded]'))
        self.assertIn(
            'the original cause', transition_message.last_error_message)
        self.assertEqual(invoice.status, 'fixed_by_hand')
        self.assertEqual(_HOOK_LOG, [], 'failure hooks ran on a fixed row')

    def test_hooks_still_run_when_the_state_matches(self):
        """Control: with the state unchanged, the row fails normally."""
        invoice, transition_message = self._row_at_max()

        self.assertTrue(finalize_stuck_attempt(transition_message.pk))

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'sp_failed')
        self.assertEqual(_HOOK_LOG, ['fcb'])


# --- The bookkeeping writes must never be what fails -----------------------

class DbSafeTextTests(TestCase):
    def test_nul_is_escaped_not_passed_through(self):
        self.assertEqual(db_safe_text('a\x00b'), 'a\\x00b')

    def test_lone_surrogate_is_replaced(self):
        # A lone surrogate lives in a Python str but cannot be encoded to
        # UTF-8, so PostgreSQL rejects the write.
        cleaned = db_safe_text('a\ud800b')
        cleaned.encode('utf-8')  # must not raise

    def test_record_error_stores_escaped_text(self):
        transition_message = TransitionMessage.objects.create(
            app_label='tests', model_name='invoice', instance_id='1',
            process_name='p', transition_name='go', queue_name='q',
        )
        transition_message.record_error(ValueError('a\x00b'))
        transition_message.refresh_from_db()
        self.assertEqual(transition_message.last_error_message, 'a\\x00b')

    def test_failure_note_fits_the_column(self):
        """An oversized note truncates to the column budget; its start (the
        label and the exception type) survives."""
        transition_message = TransitionMessage.objects.create(
            app_label='tests', model_name='invoice', instance_id='1',
            process_name='p', transition_name='go', queue_name='q',
        )
        transition_message.record_failure_side_effect_error(
            ValueError('x' * 20_000), label='failed_state write')
        transition_message.refresh_from_db()
        self.assertTrue(
            transition_message.failure_side_effect_error.startswith(
                'failed_state write: ValueError:'))
        self.assertLessEqual(
            len(transition_message.failure_side_effect_error), 10_000)


# --- failed_state must differ from in_progress_state -----------------------

class IndistinguishableFailureStateTests(TestCase):
    def test_failed_state_equal_to_in_progress_state_is_refused(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            BackgroundTransition(
                'go', sources=['draft'], target='done',
                in_progress_state='running', failed_state='running',
            )
        self.assertIn('indistinguishable', str(ctx.exception))

    def test_distinct_states_are_accepted(self):
        BackgroundTransition(
            'go', sources=['draft'], target='done',
            in_progress_state='running', failed_state='failed',
        )

    def test_sync_transitions_reject_in_progress_state(self):
        """in_progress_state is background-only. On a synchronous transition it
        left the instance busy with no row to recover it."""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            Transition(
                'go', sources=['draft'], target='done',
                in_progress_state='running',
            )
        self.assertIn('BackgroundTransition', str(ctx.exception))


# --- Engine keyword arguments are refused before the lock ------------------

class ReservedKwargProcess(Process):
    process_name = 'reserved_kwarg_proc'
    transitions = [
        Transition(
            'go', sources=['draft'], target='done',
            failed_state='rk_failed',
            side_effects=[_boom],
        ),
        Transition('act', sources=['draft'], side_effects=[_boom]),
        BackgroundTransition(
            'bg', sources=['draft'], target='done',
            in_progress_state='rk_bg_running', failed_state='rk_bg_failed',
            side_effects=[_noop],
        ),
    ]


@override_settings(DJANGO_LOGIC=_SYNC)
class ReservedKwargTests(_BindCleanup, TestCase):
    """A caller keyword named like an engine parameter (``exception``,
    ``state``) used to collide on the failure path. The real error became
    a TypeError, ``failed_state`` was never written, and the lock stayed
    until it expired.
    """

    _bound = (ReservedKwargProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, ReservedKwargProcess, state_field='status')
        cache.clear()
        self.addCleanup(cache.clear)

    def test_transition_refuses_and_leaves_no_lock(self):
        invoice = Invoice.objects.create(status='draft')
        for kwarg in ('exception', 'state'):
            with self.subTest(kwarg=kwarg):
                with self.assertRaises(TypeError) as ctx:
                    invoice.reserved_kwarg_proc.go(**{kwarg: 'anything'})
                self.assertIn(kwarg, str(ctx.exception))
                invoice.refresh_from_db()
                # Refused before the lock and before any state write.
                self.assertEqual(invoice.status, 'draft')
                self.assertFalse(
                    invoice.reserved_kwarg_proc.state.is_locked(),
                    'the refusal left the state locked',
                )

    def test_action_refuses(self):
        invoice = Invoice.objects.create(status='draft')
        with self.assertRaises(TypeError):
            invoice.reserved_kwarg_proc.act(exception='anything')

    def test_background_enqueue_refuses_before_creating_a_row(self):
        invoice = Invoice.objects.create(status='draft')
        with self.assertRaises(TypeError):
            invoice.reserved_kwarg_proc.bg(exception='anything')
        self.assertFalse(
            TransitionMessage.objects.filter(
                instance_id=str(invoice.pk)).exists())
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'draft')

    def test_an_ordinary_kwarg_still_reaches_the_hooks(self):
        """Control: the refusal is narrow."""
        seen = {}

        def capture(instance, **kwargs):
            seen.update(kwargs)

        invoice = Invoice.objects.create(status='draft')
        transition = Transition(
            'ok', sources=['draft'], target='done', side_effects=[capture])
        transition.change_state(
            invoice.reserved_kwarg_proc.state,
            payload={'exception': 'nested'})
        self.assertEqual(seen['payload'], {'exception': 'nested'})


# --- An expected concurrency guard is not an ERROR -------------------------

class ConcurrencyGuardLogLevelTests(TestCase):
    """The source recheck after the insert fires when a competing transition
    finished while this enqueue waited on the unique index. That is normal
    contention, so it logs at WARNING and wakes nobody.
    """

    def test_guard_exceptions_log_at_warning(self):
        from django_logic.commands import _log_hook_error

        with self.assertLogs('django-logic.transition', level='DEBUG') as logs:
            _log_hook_error('guard', TransitionTemporarilyUnavailable('moved'))
        self.assertEqual(
            [r.levelno for r in logs.records], [logging.WARNING], logs.output)

    def test_other_exceptions_still_log_at_error(self):
        from django_logic.commands import _log_hook_error

        with self.assertLogs('django-logic.transition', level='DEBUG') as logs:
            _log_hook_error('real', ValueError('a genuine bug'))
        self.assertEqual(
            [r.levelno for r in logs.records], [logging.ERROR], logs.output)


# --- One catchable type for "busy, retry shortly" --------------------------

class TemporarilyUnavailableBaseTests(TestCase):
    """A top-level handler must tell "busy, retry shortly" from "not allowed,
    do not retry" without importing the background subpackage. The base sits
    between the guard exceptions and ``TransitionNotAllowed``, so existing
    handlers keep working.
    """

    def test_guard_exceptions_share_the_transient_base(self):
        self.assertTrue(
            issubclass(AlreadyInProgress, TransitionTemporarilyUnavailable))

    def test_transient_base_is_a_transition_not_allowed(self):
        self.assertTrue(issubclass(
            TransitionTemporarilyUnavailable, TransitionNotAllowed))

    def test_except_ordering_separates_busy_from_forbidden(self):
        # The documented consumer pattern: catch the transient base before
        # TransitionNotAllowed to answer "busy" instead of "forbidden".
        def classify(error):
            try:
                raise error
            except TransitionTemporarilyUnavailable:
                return 'busy'
            except TransitionNotAllowed:
                return 'forbidden'

        self.assertEqual(classify(AlreadyInProgress('still running')), 'busy')
        self.assertEqual(
            classify(TransitionNotAllowed('no such transition')), 'forbidden')

    def test_already_in_progress_logs_at_warning(self):
        from django_logic.commands import _log_hook_error

        with self.assertLogs('django-logic.transition', level='DEBUG') as logs:
            _log_hook_error('guard', AlreadyInProgress('still running'))
        self.assertEqual(
            [r.levelno for r in logs.records], [logging.WARNING], logs.output)

    def test_consumer_subclass_of_transient_base_logs_at_warning(self):
        # The WARNING level keys on the base class, not on a fixed list, so a
        # consumer subclass also means "retry shortly".
        from django_logic.commands import _log_hook_error

        class ChildBusy(TransitionTemporarilyUnavailable):
            pass

        with self.assertLogs('django-logic.transition', level='DEBUG') as logs:
            _log_hook_error('guard', ChildBusy('the child is still running'))
        self.assertEqual(
            [r.levelno for r in logs.records], [logging.WARNING], logs.output)


# --- An unclassified restore failure is still counted ----------------------

class UnclassifiedRestoreFailureProcess(Process):
    process_name = 'restore_fail_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='done',
            in_progress_state='rf_running', failed_state='rf_failed',
            side_effects=[_noop],
        ),
    ]


@override_settings(DJANGO_LOGIC=_SYNC)
class UnclassifiedRestoreFailureTests(_BindCleanup, TestCase):
    """Restore treats the permanent failures (model uninstalled, row gone,
    transition renamed) as final and completes the row. Any other restore
    error must still raise ``errors_count``, or the row stays claimable
    forever.
    """

    _bound = (UnclassifiedRestoreFailureProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, UnclassifiedRestoreFailureProcess, state_field='status')
        cache.clear()
        self.addCleanup(cache.clear)

    def _row_with_corrupt_instance_id(self, **overrides):
        fields = dict(
            app_label='tests', model_name='invoice',
            instance_id='not-an-integer',
            process_name='restore_fail_proc', transition_name='go',
            queue_name='django_logic',
        )
        fields.update(overrides)
        return TransitionMessage.objects.create(**fields)

    def test_the_failure_is_counted_and_the_row_left_for_retry(self):
        transition_message = self._row_with_corrupt_instance_id()

        with self.assertRaises(ValueError):
            run_background_transition(transition_message.pk)

        transition_message.refresh_from_db()
        self.assertEqual(
            transition_message.errors_count, 1,
            'the failure was not counted')
        self.assertFalse(transition_message.is_completed)

    def test_the_row_completes_at_max_errors_instead_of_retrying_forever(self):
        transition_message = self._row_with_corrupt_instance_id(errors_count=2)

        with self.assertRaises(ValueError):
            run_background_transition(transition_message.pk)

        transition_message.refresh_from_db()
        self.assertEqual(transition_message.errors_count, 3)
        self.assertTrue(
            transition_message.is_completed, 'the row would retry forever')


# --- The runner's tree walks need the cycle guard --------------------------

class CycleBackProcess(Process):
    """Nests back into its parent, which is a legal topology."""

    process_name = 'cycle_back_proc'
    transitions = [
        BackgroundTransition(
            'inner', sources=['draft'], target='done',
            in_progress_state='cb_running', failed_state='cb_failed',
            side_effects=[_noop],
        ),
    ]


class CycleRootProcess(Process):
    process_name = 'cycle_root_proc'
    nested_processes = [CycleBackProcess]
    transitions = [
        BackgroundTransition(
            'outer', sources=['draft'], target='done',
            in_progress_state='cr_running', failed_state='cr_failed',
            side_effects=[_noop],
        ),
    ]


CycleBackProcess.nested_processes = [CycleRootProcess]  # close the cycle


class RunnerTreeWalkCycleTests(TestCase):
    """A process may nest back into a parent, so the restore lookup's walk
    over ``nested_processes`` needs the cycle guard. It used to miss it and
    raise ``RecursionError``, which left the row impossible to restore or
    complete. A transition reached through two nested paths is one shared
    object, so it must count as one match, not an ambiguity.
    """

    @staticmethod
    def _row(action_name, owner=''):
        from types import SimpleNamespace

        return SimpleNamespace(
            pk=1, transition_name=action_name, owning_process_class=owner,
        )

    def test_owner_lookup_terminates_on_a_cycle(self):
        from django_logic.background.runner import _find_transition

        root = CycleRootProcess(field_name='status', instance=Invoice())
        found = _find_transition(root, self._row(
            'inner',
            f'{CycleBackProcess.__module__}.{CycleBackProcess.__name__}',
        ))
        self.assertIsNotNone(found)
        self.assertEqual(found.action_name, 'inner')

    def test_gone_owner_falls_back_to_the_unambiguous_name(self):
        from django_logic.background.runner import _find_transition

        root = CycleRootProcess(field_name='status', instance=Invoice())
        found = _find_transition(root, self._row('inner', 'gone.RenamedProcess'))
        self.assertIsNotNone(found)
        self.assertEqual(found.action_name, 'inner')

    def test_name_lookup_terminates_on_a_cycle_and_counts_shared_once(self):
        from django_logic.background.runner import _find_transition

        root = CycleRootProcess(field_name='status', instance=Invoice())
        # 'inner' is reachable through the cycle twice, but it is one shared
        # object: one match, not an ambiguity refusal.
        found = _find_transition(root, self._row('inner'))
        self.assertIsNotNone(found)
        self.assertEqual(found.action_name, 'inner')
        self.assertIsNone(_find_transition(root, self._row('nope')))
