"""Regression pins for the fourth review pass over 0.12.0.

Every test here fails if its fix is reverted — checked by mutation, because
this release already shipped two assertions that proved nothing (a filter on
the wrong log-string case, and an `isinstance` on a non-MTI model).

The findings these pin were produced by reviewing 0.12.0's own diff, so they
sit alongside `test_issue_fixes_0_12.py` rather than inside it: that file is
the record of #178–#182, this one of what fixing them missed.
"""
import logging
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Action, Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.exceptions import SourceStateChanged
from django_logic.background.models import TransitionMessage, db_safe_text
from django_logic.background.runner import (
    abandon_timed_out_attempt,
    finalize_stuck_attempt,
    run_background_transition,
)
from django_logic.exceptions import TransitionNotAllowed
from tests.models import Invoice

_SYNC = {
    'BACKGROUND_EXECUTION': 'sync',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 0,
}


class _BindCleanup:
    """Purge the global binding registry, which is process-wide state."""

    _bound = ()

    def tearDown(self):
        for process_class in self._bound:
            ProcessManager.unbind_model_process(Invoice, process_class)
        super().tearDown()


def _noop(instance, **kwargs):
    pass


def _boom(instance, **kwargs):
    raise ValueError('boom')


# --- C1: the watchdog must re-verify staleness under the row lock ----------

class WatchdogUnderLockRecheckProcess(Process):
    process_name = 'wd_recheck_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='done',
            in_progress_state='wd_running', failed_state='wd_failed',
            side_effects=[_noop], timeout=60,
        ),
    ]


@override_settings(DJANGO_LOGIC=_SYNC)
class WatchdogUnderLockRecheckTests(_BindCleanup, TestCase):
    """The candidate scan is unsynchronised, so its staleness verdict is only
    a hint. A retry that stamps a fresh ``started_at`` between the scan and
    the lock made the one-charge guard pass — the guard compares the error to
    ``started_at``, and the NEW stamp is later than the OLD error — so a
    healthy attempt milliseconds old was charged a timeout, and at MAX_ERRORS
    terminalised out from under its live worker.
    """

    _bound = (WatchdogUnderLockRecheckProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, WatchdogUnderLockRecheckProcess, state_field='status')
        cache.clear()

    def _stale_row(self, **overrides):
        inv = Invoice.objects.create(status='wd_running')
        fields = dict(
            app_label='tests', model_name='invoice', instance_id=str(inv.pk),
            process_name='wd_recheck_proc', transition_name='go',
            queue_name='django_logic', timeout_seconds=60,
            started_at=timezone.now() - timedelta(hours=1),
        )
        fields.update(overrides)
        return inv, TransitionMessage.objects.create(**fields)

    def test_a_stale_abandoned_attempt_is_still_charged(self):
        """Positive control: the watchdog must keep doing its job."""
        _, tm = self._stale_row()

        self.assertTrue(abandon_timed_out_attempt(tm.pk))

        tm.refresh_from_db()
        self.assertEqual(tm.errors_count, 1)
        self.assertIn('[watchdog timeout]', tm.last_error_message)

    def test_a_freshly_restamped_attempt_is_not_charged(self):
        """The row was stale when scanned; a new attempt started before the
        lock was taken. Charging it burns a retry the consumer never used."""
        _, tm = self._stale_row()
        tm.record_error(ValueError('the previous attempt failed'))
        # A retry dispatch stamps the new attempt AFTER that error — exactly
        # what stamp_attempt_started commits before the attempt's atomic.
        TransitionMessage.objects.filter(pk=tm.pk).update(
            started_at=timezone.now())

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            self.assertFalse(abandon_timed_out_attempt(tm.pk))
        self.assertTrue(
            any('not stale when re-checked' in line for line in logs.output),
            logs.output,
        )

        tm.refresh_from_db()
        self.assertEqual(tm.errors_count, 1, 'the live attempt was re-charged')
        self.assertFalse(tm.is_completed)

    def test_a_restamp_at_max_errors_cannot_terminalise_a_live_attempt(self):
        """The worst outcome of the race: the row is finalized while its
        worker is still running, so the successful result lands on a
        completed row and the instance is left in failed_state."""
        inv, tm = self._stale_row(errors_count=2)
        tm.record_error(ValueError('second failure'))  # errors_count -> 3 = MAX
        TransitionMessage.objects.filter(pk=tm.pk).update(
            errors_count=2, started_at=timezone.now())

        self.assertFalse(abandon_timed_out_attempt(tm.pk))

        tm.refresh_from_db()
        inv.refresh_from_db()
        self.assertFalse(tm.is_completed)
        self.assertEqual(inv.status, 'wd_running')


# --- C2: safety-net finalizers supersede a manual fix WHOLE ----------------

#: Hook calls recorded out-of-band: the failure hooks must be observed
#: WITHOUT a model write, because the point of the C2 pin is that they never
#: run (a write would also be rolled back by the finalizer's savepoint and
#: could pass for "did not run").
_HOOK_LOG: list = []


def _record_fse(instance, **kwargs):
    _HOOK_LOG.append('fse')


def _record_fcb(instance, **kwargs):
    _HOOK_LOG.append('fcb')


class SupersedeParityProcess(Process):
    process_name = 'supersede_parity_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='done',
            in_progress_state='sp_running', failed_state='sp_failed',
            side_effects=[_noop], timeout=60,
            failure_side_effects=[_record_fse],
            failure_callbacks=[_record_fcb],
        ),
    ]


@override_settings(DJANGO_LOGIC=_SYNC)
class SupersedeParityTests(_BindCleanup, TestCase):
    """Phase 2 supersedes when the instance was moved externally: no hooks, a
    ``[superseded]`` marker, row completed. The safety-net finalizers guarded
    only the ``failed_state`` WRITE — they still ran failure_side_effects and
    failure_callbacks against an instance an operator had already fixed
    (destructive cleanup, report-back callbacks) and completed the row with
    nothing explaining why.
    """

    _bound = (SupersedeParityProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, SupersedeParityProcess, state_field='status')
        cache.clear()
        _HOOK_LOG.clear()

    def _row_at_max(self, status='sp_running', **overrides):
        inv = Invoice.objects.create(status=status)
        fields = dict(
            app_label='tests', model_name='invoice', instance_id=str(inv.pk),
            process_name='supersede_parity_proc', transition_name='go',
            queue_name='django_logic', errors_count=3, timeout_seconds=60,
        )
        fields.update(overrides)
        return inv, TransitionMessage.objects.create(**fields)

    def test_detect_stuck_supersedes_and_runs_no_hooks(self):
        inv, tm = self._row_at_max(status='fixed_by_hand')
        tm.record_error(ValueError('the original cause'))

        self.assertTrue(finalize_stuck_attempt(tm.pk))

        tm.refresh_from_db()
        inv.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertTrue(tm.last_error_message.startswith('[superseded]'))
        self.assertIn('the original cause', tm.last_error_message)
        self.assertEqual(inv.status, 'fixed_by_hand')
        self.assertEqual(_HOOK_LOG, [], 'failure hooks ran on a fixed row')

    def test_watchdog_finalizer_supersedes_and_runs_no_hooks(self):
        inv, tm = self._row_at_max(
            status='fixed_by_hand', errors_count=2,
            started_at=timezone.now() - timedelta(hours=1),
        )

        self.assertTrue(abandon_timed_out_attempt(tm.pk))

        tm.refresh_from_db()
        inv.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertIn('[superseded]', tm.last_error_message)
        self.assertEqual(inv.status, 'fixed_by_hand')
        self.assertEqual(_HOOK_LOG, [], 'failure hooks ran on a fixed row')

    def test_hooks_still_run_when_the_state_matches(self):
        """Positive control: containment is the normal outcome."""
        inv, tm = self._row_at_max()

        self.assertTrue(finalize_stuck_attempt(tm.pk))

        inv.refresh_from_db()
        self.assertEqual(inv.status, 'sp_failed')
        self.assertEqual(_HOOK_LOG, ['fse', 'fcb'])


# --- C4 / B-K3 / E6: the bookkeeping must never be what fails -------------

class DbSafeTextTests(TestCase):
    def test_nul_is_escaped_not_passed_through(self):
        self.assertEqual(db_safe_text('a\x00b'), 'a\\x00b')

    def test_lone_surrogate_is_replaced(self):
        # Survives in a Python str (surrogateescape decoding) but cannot be
        # encoded for the wire, so PostgreSQL rejects the write.
        cleaned = db_safe_text('a\ud800b')
        cleaned.encode('utf-8')  # must not raise

    def test_record_error_stores_escaped_text(self):
        tm = TransitionMessage.objects.create(
            app_label='tests', model_name='invoice', instance_id='1',
            process_name='p', transition_name='go', queue_name='q',
        )
        tm.record_error(ValueError('a\x00b'))
        tm.refresh_from_db()
        self.assertEqual(tm.last_error_message, 'a\\x00b')

    def test_appended_failure_note_keeps_the_newest_entry(self):
        """Truncation keeps the head, so appending near the limit silently
        dropped the note just added — the newest, most relevant diagnostic."""
        tm = TransitionMessage.objects.create(
            app_label='tests', model_name='invoice', instance_id='1',
            process_name='p', transition_name='go', queue_name='q',
            failure_side_effect_error='x' * 9_990,
        )
        tm.record_failure_side_effect_error(
            ValueError('the newest problem'), label='failed_state write')
        tm.refresh_from_db()
        self.assertIn('the newest problem', tm.failure_side_effect_error)
        self.assertLessEqual(len(tm.failure_side_effect_error), 10_000)


# --- C7: failed_state must be distinguishable from in_progress_state ------

class IndistinguishableFailureStateTests(TestCase):
    def test_failed_state_equal_to_in_progress_state_is_refused(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            Transition(
                'go', sources=['draft'], target='done',
                in_progress_state='running', failed_state='running',
            )
        self.assertIn('indistinguishable', str(ctx.exception))

    def test_distinct_states_are_accepted(self):
        Transition(
            'go', sources=['draft'], target='done',
            in_progress_state='running', failed_state='failed',
        )


# --- F4: engine-parameter kwargs are refused BEFORE the lock -------------

class ReservedKwargProcess(Process):
    process_name = 'reserved_kwarg_proc'
    transitions = [
        Transition(
            'go', sources=['draft'], target='done',
            in_progress_state='rk_running', failed_state='rk_failed',
            side_effects=[_boom],
        ),
        Action('act', sources=['draft'], side_effects=[_boom]),
        BackgroundTransition(
            'bg', sources=['draft'], target='done',
            in_progress_state='rk_bg_running', failed_state='rk_bg_failed',
            side_effects=[_noop],
        ),
    ]


@override_settings(DJANGO_LOGIC=_SYNC)
class ReservedKwargTests(_BindCleanup, TestCase):
    """``go(exception=…)`` used to reach ``fail_transition(state, error,
    **kwargs)`` and raise "multiple values for argument 'exception'" on the
    FAILURE path: failed_state was never applied, the real error was replaced
    by the TypeError, and the lock leaked until its TTL. ``deferrable`` did
    the same from inside the ``finally``.
    """

    _bound = (ReservedKwargProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, ReservedKwargProcess, state_field='status')
        cache.clear()

    def test_transition_refuses_and_leaves_no_lock(self):
        inv = Invoice.objects.create(status='draft')
        for kwarg in ('exception', 'deferrable', 'state'):
            with self.subTest(kwarg=kwarg):
                with self.assertRaises(TypeError) as ctx:
                    inv.reserved_kwarg_proc.go(**{kwarg: 'anything'})
                self.assertIn(kwarg, str(ctx.exception))
                inv.refresh_from_db()
                # Refused before the lock and before any state write.
                self.assertEqual(inv.status, 'draft')
                self.assertFalse(
                    inv.reserved_kwarg_proc.state.is_locked(),
                    'the refusal leaked the state lock',
                )

    def test_action_refuses(self):
        inv = Invoice.objects.create(status='draft')
        with self.assertRaises(TypeError):
            inv.reserved_kwarg_proc.act(exception='anything')

    def test_background_phase_one_refuses_before_creating_a_row(self):
        inv = Invoice.objects.create(status='draft')
        with self.assertRaises(TypeError):
            inv.reserved_kwarg_proc.bg(exception='anything')
        self.assertFalse(
            TransitionMessage.objects.filter(instance_id=str(inv.pk)).exists())
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'draft')

    def test_an_ordinary_kwarg_still_reaches_the_hooks(self):
        """Control: the refusal is narrow."""
        seen = {}

        def capture(instance, **kwargs):
            seen.update(kwargs)

        inv = Invoice.objects.create(status='draft')
        transition = Transition(
            'ok', sources=['draft'], target='done', side_effects=[capture])
        transition.change_state(
            inv.reserved_kwarg_proc.state, payload={'exception': 'nested'})
        self.assertEqual(seen['payload'], {'exception': 'nested'})


# --- #154: an expected concurrency guard is not an ERROR ------------------

class ConcurrencyGuardLogLevelTests(TestCase):
    """The post-create source recheck fires when a competing flight finished
    while this phase 1 waited on the partial unique index — the guard working
    as designed. At ERROR it pages an on-call for healthy contention.
    """

    def test_source_state_changed_is_a_transition_not_allowed(self):
        # Consumers catching the base class must keep working.
        self.assertTrue(issubclass(SourceStateChanged, TransitionNotAllowed))

    def test_guard_exceptions_log_at_warning(self):
        from django_logic.commands import _log_hook_error

        with self.assertLogs('django-logic.transition', level='DEBUG') as logs:
            _log_hook_error('guard', SourceStateChanged('moved'))
        self.assertEqual(
            [r.levelno for r in logs.records], [logging.WARNING], logs.output)

    def test_other_exceptions_still_log_at_error(self):
        from django_logic.commands import _log_hook_error

        with self.assertLogs('django-logic.transition', level='DEBUG') as logs:
            _log_hook_error('real', ValueError('a genuine bug'))
        self.assertEqual(
            [r.levelno for r in logs.records], [logging.ERROR], logs.output)


# --- C3: a restore failure the engine did not classify is still accounted --

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
    """``_restore`` classifies the PERMANENT failures (model uninstalled, row
    gone, transition renamed) as ``_RestoreError`` and completes the row.
    Everything else — a consumer ``process`` property raising, a corrupt
    ``instance_id`` failing pk coercion — escaped phase 2 with
    ``errors_count`` still 0, so the starter re-dispatched it forever: the
    same unaccounted infinite-retry class #178 closed for state writes.
    """

    _bound = (UnclassifiedRestoreFailureProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, UnclassifiedRestoreFailureProcess, state_field='status')
        cache.clear()

    def _row_with_corrupt_instance_id(self, **overrides):
        fields = dict(
            app_label='tests', model_name='invoice',
            instance_id='not-an-integer',
            process_name='restore_fail_proc', transition_name='go',
            queue_name='django_logic',
        )
        fields.update(overrides)
        return TransitionMessage.objects.create(**fields)

    def test_it_is_charged_and_left_for_retry(self):
        tm = self._row_with_corrupt_instance_id()

        with self.assertRaises(ValueError):
            run_background_transition(tm.pk)

        tm.refresh_from_db()
        self.assertEqual(tm.errors_count, 1, 'the failure was not accounted')
        self.assertFalse(tm.is_completed)

    def test_it_terminates_at_max_errors_instead_of_retrying_forever(self):
        tm = self._row_with_corrupt_instance_id(errors_count=2)

        with self.assertRaises(ValueError):
            run_background_transition(tm.pk)

        tm.refresh_from_db()
        self.assertEqual(tm.errors_count, 3)
        self.assertTrue(tm.is_completed, 'the row would retry forever')


# --- D1: a rolled-back attempt must release nested deferred unlocks -------

_VICTIM: dict = {}


class NestedVictimProcess(Process):
    process_name = 'nested_victim_proc'
    transitions = [
        Transition('approve', sources=['draft'], target='approved'),
    ]


def _drive_the_victim(instance, **kwargs):
    victim = Invoice.objects.get(pk=_VICTIM['pk'])
    NestedVictimProcess(field_name='status', instance=victim).approve()


class DeferLeakProcess(Process):
    process_name = 'defer_leak_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='done',
            in_progress_state='dl_running', failed_state='dl_failed',
            side_effects=[_drive_the_victim, _boom],
        ),
    ]


class Phase2DeferredUnlockTests(TransactionTestCase):
    """Side-effects are consumer code, and the recipes encourage driving other
    instances from them. Under ``DEFER_UNLOCK_UNTIL_COMMIT`` such a nested sync
    transition registers its unlock on ``transaction.on_commit`` INSIDE the
    phase-2 attempt savepoint. A later side-effect failure rolls that savepoint
    back, Django discards the hook with it — and the outer transaction still
    commits the bookkeeping, so the victim's lock was held until its TTL
    (7200s by default) while its state write was rolled back. Every hook bundle
    already routed through ``_run_in_savepoint``; the attempt savepoint was the
    one raw ``atomic`` left.
    """

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, DeferLeakProcess, state_field='status')
        ProcessManager.bind_model_process(
            Invoice, NestedVictimProcess, state_field='status')
        cache.clear()
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, DeferLeakProcess)
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, NestedVictimProcess)

    def test_attempt_rollback_releases_the_nested_instances_lock(self):
        from django_logic.state import State
        from tests import dl_settings

        driver = Invoice.objects.create(status='draft')
        victim = Invoice.objects.create(status='draft')
        _VICTIM['pk'] = victim.pk

        with override_settings(DJANGO_LOGIC=dl_settings(
            BACKGROUND_EXECUTION='sync',
            DEFER_UNLOCK_UNTIL_COMMIT=True,
            TRANSITION_MESSAGE_MAX_ERRORS=3,
        )):
            with self.assertRaises(ValueError):
                driver.defer_leak_proc.go()

        victim.refresh_from_db()
        # The nested write rolled back with the attempt savepoint...
        self.assertEqual(victim.status, 'draft')
        # ...so its lock guards nothing and must have been released.
        victim_state = State(
            victim, 'status', process_name='nested_victim_proc')
        self.assertFalse(
            victim_state.is_locked(),
            'the rolled-back attempt leaked the nested instance lock until TTL',
        )


# --- F2: the runner's own tree walks need the cycle guard ----------------

class CycleBackProcess(Process):
    """Nested back into its parent — a topology the sync walk blesses."""

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
    """#180 made the sync walk cycle-safe and its own test blesses A→B→A, but
    two raw ``nested_processes`` walks survived in the phase-2 restore path —
    on the blank/stale ``owning_process_class`` fall-throughs the caller is
    written to handle gracefully. A cyclic topology hit ``RecursionError``
    there instead, so the row could never be restored or finalized.
    """

    def test_owner_lookup_terminates_on_a_cycle(self):
        from django_logic.background.runner import (
            _find_background_transition_in_owner,
        )

        root = CycleRootProcess(field_name='status', instance=Invoice())
        found = _find_background_transition_in_owner(
            root, 'inner',
            f'{CycleBackProcess.__module__}.{CycleBackProcess.__name__}',
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.action_name, 'inner')

    def test_owner_lookup_terminates_when_the_owner_is_gone(self):
        from django_logic.background.runner import (
            _find_background_transition_in_owner,
        )

        root = CycleRootProcess(field_name='status', instance=Invoice())
        self.assertIsNone(_find_background_transition_in_owner(
            root, 'inner', 'gone.RenamedProcess'))

    def test_name_lookup_terminates_on_a_cycle(self):
        from django_logic.background.runner import _background_transitions_named

        root = CycleRootProcess(field_name='status', instance=Invoice())
        matches = _background_transitions_named(root, 'inner')
        self.assertEqual(len(matches), 1, 'a shared transition counted twice')
        self.assertEqual(
            _background_transitions_named(root, 'nope'), [])
