"""recover_stranded_states — the fifth safety-net task (#136).

A hard-killed synchronous transition leaves its instance parked in the
transition's ``in_progress_state``: the lock self-expires and the
implicit-source rule keeps it re-drivable, but nothing ACTS — no failure
hooks, no alert. These tests pin the sweep's contract: provably-stranded
instances (in-progress + unlocked + no uncompleted TransitionMessage) are
driven through the owning transition's failure path; everything else is
left alone.
"""
from unittest import mock

from django.test import TestCase

from django_logic.background.dispatch import (
    _reset_warned,
    recover_stranded_states,
)
from django_logic.background.models import TransitionMessage
from django_logic.background.settings import beat_schedule
from django_logic.process import Process, ProcessManager
from django_logic.state import State
from django_logic.transition import Action, Transition
from tests.models import Invoice

FAILURE_LOG = []


def record_failure_side_effect(instance, **kwargs):
    FAILURE_LOG.append(('side_effect', instance.pk, str(kwargs.get('exception') or '')))


def record_failure_callback(instance, **kwargs):
    FAILURE_LOG.append(('callback', instance.pk))


def context_requiring_side_effect(instance, context, **kwargs):
    """The engine guarantees `context` to hooks (``_init_transition_context``
    on the sync path, the phase-2 restore in the runner). A hook declared
    with this signature TypeErrors if a caller omits it — and the hook
    runners swallow that, silently skipping the hook."""
    FAILURE_LOG.append(('context_hook', instance.pk, context))


def raising_failure_side_effect(instance, **kwargs):
    FAILURE_LOG.append(('raiser', instance.pk))
    raise ValueError('failure hook exploded')


class _StrandedProcess(Process):
    process_name = 'stranded_process'
    transitions = [
        Transition('sync_up', sources=['draft'], target='done',
                   in_progress_state='syncing',
                   failed_state='failed',
                   failure_side_effects=[record_failure_side_effect,
                                         context_requiring_side_effect],
                   failure_callbacks=[record_failure_callback]),
        Transition('sync_boom', sources=['ready'], target='shipped',
                   in_progress_state='shipping',
                   failed_state='ship_failed',
                   failure_side_effects=[raising_failure_side_effect]),
        Transition('archive', sources=['done'], target='archived',
                   in_progress_state='archiving'),  # no failed_state
        # A namesake of 'archive' parking in a DIFFERENT state, also
        # without failed_state — each parked backlog must warn on its
        # own (the warn-once key includes in_progress_state).
        Transition('archive', sources=['cold'], target='archived',
                   in_progress_state='cold_archiving'),  # no failed_state
        # An Action ACCEPTS in_progress_state (implicit source only —
        # never written) and failed_state; it must not be a sweep
        # candidate: its fail_transition holds no lock, so it neither
        # unlocks nor writes failed_state while the state is locked.
        Action('audit', sources=['done'],
               in_progress_state='auditing',
               failed_state='audit_failed',
               failure_side_effects=[record_failure_side_effect],
               failure_callbacks=[record_failure_callback]),
    ]


class RecoverStrandedStatesTests(TestCase):
    def setUp(self):
        super().setUp()
        FAILURE_LOG.clear()
        # The missing-failed_state warning fires once per process
        # lifetime; reset so each test observes it deterministically.
        _reset_warned()
        ProcessManager.bind_model_process(
            Invoice, _StrandedProcess, state_field='status')

    def tearDown(self):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class is not _StrandedProcess
        ]
        if 'stranded_process' in vars(Invoice):
            delattr(Invoice, 'stranded_process')
        super().tearDown()

    @staticmethod
    def _strand(status):
        """A crashed sync transition: state persisted mid-flight, no lock
        (expired), no TransitionMessage (sync transitions write none)."""
        invoice = Invoice.objects.create(status='draft')
        Invoice.objects.filter(pk=invoice.pk).update(status=status)
        invoice.refresh_from_db()
        return invoice

    def test_stranded_instance_is_driven_to_failed_state_with_hooks(self):
        invoice = self._strand('syncing')

        with self.assertLogs('django-logic', level='ERROR') as logs:
            recovered = recover_stranded_states()

        self.assertEqual(recovered, 1)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'failed')
        kinds = [entry[0] for entry in FAILURE_LOG]
        self.assertIn('side_effect', kinds)
        self.assertIn('callback', kinds)
        # the synthetic error carries the marker, and the recovery is loud
        self.assertTrue(any('[stranded]' in e[2] for e in FAILURE_LOG
                            if e[0] == 'side_effect'))
        self.assertTrue(any('recovered' in line for line in logs.output))

    def test_context_requiring_hooks_receive_context(self):
        """Hooks declared `def fn(instance, context, **kwargs)` are an
        engine-supported signature; the sweep must pass `context` like
        every other fail_transition caller, or the hook TypeErrors and
        the hook runner swallows it — silently skipped."""
        invoice = self._strand('syncing')
        self.assertEqual(recover_stranded_states(), 1)
        self.assertIn(('context_hook', invoice.pk, {}), FAILURE_LOG)

    def test_multiple_stranded_instances_recovered_in_one_sweep(self):
        a = self._strand('syncing')
        b = self._strand('syncing')
        c = self._strand('syncing')
        TransitionMessage.objects.create(
            app_label='tests', model_name='invoice',
            instance_id=str(c.pk), process_name='stranded_process',
            transition_name='sync_up', queue_name='q',
        )

        self.assertEqual(recover_stranded_states(), 2)

        for invoice, expected in ((a, 'failed'), (b, 'failed'),
                                  (c, 'syncing')):
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, expected)
        recovered_pks = {e[1] for e in FAILURE_LOG if e[0] == 'side_effect'}
        self.assertEqual(recovered_pks, {a.pk, b.pk})

    def test_raising_failure_side_effect_is_swallowed_and_still_counts(self):
        """FailureSideEffects swallow hook exceptions: recovery must
        still write failed_state, count the instance, and release the
        lock exactly once (fail_transition's finally)."""
        invoice = self._strand('shipping')

        self.assertEqual(recover_stranded_states(), 1)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'ship_failed')
        self.assertIn(('raiser', invoice.pk),
                      [(e[0], e[1]) for e in FAILURE_LOG])
        self.assertFalse(State(invoice, 'status').is_locked())

    def test_rows_hidden_by_a_filtered_default_manager_are_recovered(self):
        """The sweep scans via _base_manager (like State.get_persisted_state
        and the phase-2 restore): a soft-deleted row stranded in an
        in_progress_state must not be invisible to recovery. Uses the
        issue-#90 fixture whose default manager hides archived rows."""
        from tests.background.models import ArchivableWidget
        widget = ArchivableWidget.all_objects.create(
            archived=True, status='finishing')

        self.assertEqual(recover_stranded_states(), 1)

        widget.refresh_from_db()
        self.assertEqual(widget.status, 'finish_failed')
        self.assertFalse(State(widget, 'status').is_locked())

    def test_locked_instance_is_skipped(self):
        invoice = self._strand('syncing')
        state = State(invoice, 'status')
        self.assertTrue(state.lock())
        try:
            self.assertEqual(recover_stranded_states(), 0)
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, 'syncing')
            self.assertEqual(FAILURE_LOG, [])
        finally:
            state.unlock()

    def test_instance_with_uncompleted_transition_message_is_skipped(self):
        invoice = self._strand('syncing')
        TransitionMessage.objects.create(
            app_label='tests', model_name='invoice',
            instance_id=str(invoice.pk), process_name='stranded_process',
            transition_name='sync_up', queue_name='q',
        )
        self.assertEqual(recover_stranded_states(), 0)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'syncing')

    def test_sibling_process_transition_message_does_not_block_recovery(self):
        """The in-flight shield is per process — same scope as
        ``_ensure_no_background_in_flight`` and the partial unique
        constraint. An uncompleted TransitionMessage for a *different*
        process on the same instance must not delay recovering a
        stranded sync transition on this one."""
        invoice = self._strand('syncing')
        TransitionMessage.objects.create(
            app_label='tests', model_name='invoice',
            instance_id=str(invoice.pk),
            process_name='unrelated_sibling_process',
            transition_name='something_else', queue_name='q',
        )

        self.assertEqual(recover_stranded_states(), 1)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'failed')
        self.assertTrue(any('[stranded]' in e[2] for e in FAILURE_LOG
                            if e[0] == 'side_effect'))

    def test_no_failed_state_warns_and_leaves_redrivable(self):
        invoice = self._strand('archiving')

        with self.assertLogs('django-logic', level='WARNING') as logs:
            self.assertEqual(recover_stranded_states(), 0)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'archiving')
        self.assertTrue(any('no failed_state' in line for line in logs.output))
        # the implicit-source rule keeps it re-drivable
        invoice.stranded_process.archive()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'archived')

    def test_no_failed_state_warns_once_per_process_not_per_candidate(self):
        """#145: the missing-failed_state condition is a property of the
        TRANSITION, so the sweep warns exactly once per process lifetime
        — not per candidate per run — and never locks or touches the
        parked instances (pre-fix it took the state lock per candidate
        before discovering there was nothing to recover to)."""
        a = self._strand('archiving')
        b = self._strand('archiving')

        with self.assertLogs('django-logic', level='WARNING') as first:
            self.assertEqual(recover_stranded_states(), 0)
        warnings = [line for line in first.output
                    if 'no failed_state' in line and "'archive'" in line
                    and 'tests.Invoice' in line and "'archiving'" in line]
        self.assertEqual(len(warnings), 1,
                         'one warning per transition, not per candidate')
        # observable: the parked backlog size is in the warning
        self.assertIn('2 candidate', warnings[0])

        # Second sweep: no repeat warning, and the sweep never even
        # reaches the per-instance recovery (no lock is ever taken).
        with self.assertNoLogs('django-logic', level='WARNING'):
            with mock.patch(
                'django_logic.background.dispatch._recover_stranded_instance'
            ) as recover_one:
                self.assertEqual(recover_stranded_states(), 0)
        recover_one.assert_not_called()

        for invoice in (a, b):
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, 'archiving', 'left as-is')
            self.assertFalse(State(invoice, 'status').is_locked(),
                             'never locked')

    def test_namesakes_with_distinct_in_progress_states_each_warn(self):
        """The warn-once key includes in_progress_state: namesake
        transitions parking candidates in different states are different
        parked backlogs — the second must not be silenced by the first."""
        self._strand('archiving')
        self._strand('cold_archiving')

        with self.assertLogs('django-logic', level='WARNING') as logs:
            self.assertEqual(recover_stranded_states(), 0)
        warnings = [line for line in logs.output
                    if 'no failed_state' in line and "'archive'" in line
                    and 'tests.Invoice' in line]
        self.assertEqual(len(warnings), 2,
                         'one warning per parked state, not per action name')
        self.assertTrue(any("'archiving'" in w for w in warnings))
        self.assertTrue(any("'cold_archiving'" in w for w in warnings))

    def test_large_backlog_is_fully_recovered_in_bounded_pages(self):
        """#145 regression: the sweep must page through candidates by
        pk-keyset instead of materialising every pk up front — and every
        stranded instance across multiple pages must still be recovered
        (recovered rows leave the state between pages; the keyset must
        not skip anyone)."""
        count = 120  # > 2 * the patched _TM_SCAN_CHUNK
        Invoice.objects.bulk_create(
            Invoice(status='syncing') for _ in range(count))

        with mock.patch('django_logic.background.dispatch._TM_SCAN_CHUNK', 50):
            self.assertEqual(recover_stranded_states(), count)

        self.assertEqual(Invoice.objects.filter(status='failed').count(),
                         count)
        self.assertEqual(Invoice.objects.filter(status='syncing').count(), 0)

    def test_healthy_instances_are_untouched(self):
        Invoice.objects.create(status='draft')
        Invoice.objects.create(status='done')
        self.assertEqual(recover_stranded_states(), 0)
        self.assertEqual(FAILURE_LOG, [])

    def test_action_declared_in_progress_state_is_not_a_candidate(self):
        """An Action never writes its in_progress_state, so an instance
        sitting there was not stranded BY the Action — and driving
        Action.fail_transition under the sweep's lock would skip the
        failed_state write, run spurious failure hooks, and leak the
        lock until LOCK_TIMEOUT (it holds no lock, so it releases none).
        The sweep must skip Action candidates entirely."""
        invoice = self._strand('auditing')

        self.assertEqual(recover_stranded_states(), 0)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'auditing', 'left untouched')
        self.assertEqual(FAILURE_LOG, [], 'no spurious failure hooks')
        self.assertFalse(State(invoice, 'status').is_locked(),
                         'no lock leaked behind')

    def test_recovered_instance_rejoins_the_normal_flow(self):
        invoice = self._strand('syncing')
        recover_stranded_states()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'failed')
        # failed is a plain state here; re-drive from a valid source works
        Invoice.objects.filter(pk=invoice.pk).update(status='draft')
        invoice.refresh_from_db()
        invoice.stranded_process.sync_up()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'done')

    def test_beat_schedule_includes_the_fifth_task(self):
        schedule = beat_schedule()
        entry = schedule.get('django-logic-recover-stranded')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['task'], 'django_logic.recover_stranded_states')


class GuardedRecoveryTests(TestCase):
    """The per-instance recovery guards (Bugbot HIGH on #137): recovery
    runs under the state lock with phase-2-state-guard semantics, so a
    re-drive or manual fix that wins the race is never clobbered and no
    foreign lock is ever released."""

    def setUp(self):
        super().setUp()
        FAILURE_LOG.clear()
        ProcessManager.bind_model_process(
            Invoice, _StrandedProcess, state_field='status')
        from django_logic.process import ProcessManager as PM
        self.binding = next(b for b in PM.bindings
                            if b.process_class is _StrandedProcess)
        self.transition = next(t for t in _StrandedProcess.transitions
                               if t.action_name == 'sync_up')

    def tearDown(self):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class is not _StrandedProcess
        ]
        if 'stranded_process' in vars(Invoice):
            delattr(Invoice, 'stranded_process')
        super().tearDown()

    def _recover_one(self, pk):
        from django_logic.background.dispatch import _recover_stranded_instance
        return _recover_stranded_instance(self.binding, self.transition, pk)

    def test_state_moved_under_the_race_window_wins(self):
        """The exact Bugbot scenario: the candidate was scanned as
        stranded, but by recovery time another writer moved the state —
        the under-lock re-read must make their write win."""
        invoice = Invoice.objects.create(status='draft')
        Invoice.objects.filter(pk=invoice.pk).update(status='syncing')
        # the "winning" concurrent writer: a manual fix back to draft
        Invoice.objects.filter(pk=invoice.pk).update(status='draft')

        self.assertFalse(self._recover_one(invoice.pk))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'draft', 'the manual fix survives')
        self.assertEqual(FAILURE_LOG, [])
        self.assertFalse(State(invoice, 'status').is_locked(),
                         'the sweep released the lock it took')

    def test_background_message_appearing_in_the_window_is_respected(self):
        invoice = Invoice.objects.create(status='draft')
        Invoice.objects.filter(pk=invoice.pk).update(status='syncing')
        TransitionMessage.objects.create(
            app_label='tests', model_name='invoice',
            instance_id=str(invoice.pk), process_name='stranded_process',
            transition_name='sync_up', queue_name='q',
        )
        self.assertFalse(self._recover_one(invoice.pk))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'syncing')
        self.assertFalse(State(invoice, 'status').is_locked())

    def test_lock_held_by_live_execution_blocks_recovery(self):
        invoice = Invoice.objects.create(status='draft')
        Invoice.objects.filter(pk=invoice.pk).update(status='syncing')
        state = State(invoice, 'status')
        self.assertTrue(state.lock())
        try:
            self.assertFalse(self._recover_one(invoice.pk))
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, 'syncing')
            self.assertTrue(state.is_locked(),
                            "the sweep must not release a lock it doesn't own")
        finally:
            state.unlock()

    def test_successful_recovery_leaves_no_lock_behind(self):
        invoice = Invoice.objects.create(status='draft')
        Invoice.objects.filter(pk=invoice.pk).update(status='syncing')
        self.assertTrue(self._recover_one(invoice.pk))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'failed')
        self.assertFalse(State(invoice, 'status').is_locked(),
                         'fail_transition released the transferred lock')

    def test_phase2_completion_landing_between_the_guards_is_respected(self):
        """Guard ORDER pin. Phase-2 completion holds no state lock — it
        commits its state write and is_completed atomically. A completion
        landing at the under-lock message check must not be clobbered:
        the message check runs FIRST, so the later persisted-state read
        observes the committed result. (With the guards reversed, the
        sweep reads the pre-commit state, then finds no uncompleted
        message, and overwrites a SUCCESSFUL re-drive with failed_state.)"""
        invoice = Invoice.objects.create(status='draft')
        Invoice.objects.filter(pk=invoice.pk).update(status='syncing')

        real_filter = TransitionMessage.objects.filter

        def completion_lands_now(*args, **kwargs):
            # the re-drive's phase 2 commits target + completed message
            # right as the sweep runs its under-lock message check
            Invoice.objects.filter(pk=invoice.pk).update(status='done')
            return real_filter(*args, **kwargs)

        with mock.patch.object(TransitionMessage.objects, 'filter',
                               side_effect=completion_lands_now):
            self.assertFalse(self._recover_one(invoice.pk))

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'done',
                         'the successful re-drive result survives')
        self.assertEqual(FAILURE_LOG, [])
        self.assertFalse(State(invoice, 'status').is_locked())

    def test_sweep_uses_the_process_declared_state_class(self):
        """The sweep must lock via the bound process's state_class — a
        RedisState stores the state VALUE under the lock key, so locking
        with the plain base State would make concurrent readers see the
        lock payload (True) as the state for the whole recovery window."""
        lock_calls = []

        class RecordingState(State):
            def lock(self, timeout=None):
                lock_calls.append(type(self).__name__)
                return super().lock(timeout)

        invoice = Invoice.objects.create(status='draft')
        Invoice.objects.filter(pk=invoice.pk).update(status='syncing')

        with mock.patch.object(_StrandedProcess, 'state_class',
                               RecordingState, create=True):
            self.assertTrue(self._recover_one(invoice.pk))

        self.assertEqual(lock_calls, ['RecordingState'])
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'failed')
