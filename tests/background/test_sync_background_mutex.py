"""A synchronous transition and a background transition never interleave.

The mutex is scoped to one instance plus one process:

* While an uncompleted ``TransitionMessage`` exists, ``Transition.change_state``
  raises ``TransitionNotAllowed`` under the lock and releases the lock on the
  way out.
* While a synchronous transition holds the cache lock,
  ``BackgroundTransition.change_state`` refuses to enqueue with
  ``TransitionNotAllowed("State is locked")`` and creates no row.
* ``BackgroundTransition.change_state`` releases the lock in a finally, on
  rejection and on success alike.
* A plain ``Action`` is not gated on its success path: it changes no state,
  takes no lock, and ignores background work in progress. Its failure path is
  the exception — while an uncompleted row exists the worker owns the state
  field, so the ``failed_state`` write is skipped.
"""
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError
from django.test import (
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone

from django_logic import Action
from django_logic.background import in_flight, sync_execution
from django_logic.background.exceptions import AlreadyInProgress, SourceStateChanged
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import (
    TransitionNotAllowed,
    TransitionTemporarilyUnavailable,
)
from django_logic.state import State
from tests.background.models import Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)


def _make_row(widget, process_name='process', is_completed=False):
    """Create a TransitionMessage row directly — the same row enqueue leaves
    behind while the worker still has to execute it."""
    return TransitionMessage.objects.create(
        app_label='bg_tests',
        model_name='widget',
        instance_id=str(widget.pk),
        process_name=process_name,
        transition_name='fulfil',
        queue_name='django_logic.critical',
        is_completed=is_completed,
    )


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class SyncTransitionGatedByTransitionMessageTests(TestCase):
    """The uncompleted TransitionMessage row gates synchronous transitions."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.widget = Widget.objects.create()  # status='draft'

    def test_uncompleted_row_blocks_sync_transition_and_releases_lock(self):
        # A background transition is in progress on this instance and process,
        # so the synchronous 'cancel' is refused under the lock.
        _make_row(self.widget)

        with self.assertRaises(TransitionNotAllowed) as ctx:
            self.widget.process.cancel()

        self.assertIn(
            'background transition is in progress', str(ctx.exception)
        )
        # Transition.change_state must unlock before it re-raises, or the
        # instance stays locked with nothing left to unlock it.
        state = State(self.widget, 'status', 'process')
        self.assertFalse(state.is_locked())
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')

    def test_uncompleted_row_for_other_process_does_not_block(self):
        # The gate is scoped per process: an independent state machine's row
        # must not block this process.
        _make_row(self.widget, process_name='other_process')

        self.widget.process.cancel()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'cancelled')

    def test_completed_row_does_not_block(self):
        # Only uncompleted rows gate anything — finished background work does
        # not.
        _make_row(self.widget, is_completed=True)

        self.widget.process.cancel()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'cancelled')

    def test_sync_action_is_not_gated_by_uncompleted_row(self):
        # A plain Action changes no state and takes no lock, so it runs even
        # while background work is in progress on the same instance + process.
        _make_row(self.widget)
        ran = []

        def poke_side_effect(instance, **kwargs):
            ran.append(instance.pk)

        action = Action('poke', sources=['draft'], side_effects=[poke_side_effect])
        action.change_state(State(self.widget, 'status', 'process'))

        self.assertEqual(ran, [self.widget.pk])
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')  # Actions never move state
        self.assertFalse(State(self.widget, 'status', 'process').is_locked())

    def test_gate_raises_the_transient_type(self):
        # The refusal clears once the background work finishes, so a generic
        # handler must be able to answer 409 "retry shortly", not 400.
        _make_row(self.widget)

        with self.assertRaises(TransitionTemporarilyUnavailable):
            self.widget.process.cancel()

    def test_stale_uncompleted_row_is_not_transient(self):
        # A row untouched past the retry window is stranded, not busy. "Retry
        # shortly" would be wrong forever, and demoting the log to WARNING
        # would stop a stuck instance from paging anyone.
        row = _make_row(self.widget)
        TransitionMessage.objects.filter(pk=row.pk).update(
            modified=timezone.now() - timedelta(hours=2))

        with self.assertRaises(TransitionNotAllowed) as ctx:
            self.widget.process.cancel()

        self.assertNotIsInstance(
            ctx.exception, TransitionTemporarilyUnavailable)
        self.assertIn('stranded', str(ctx.exception))
        # The refusal path must still release the lock.
        self.assertFalse(State(self.widget, 'status', 'process').is_locked())

    def test_attempt_past_its_timeout_and_retry_window_is_stranded(self):
        # The timeout has passed, nothing was recorded since, and no worker
        # holds the row: the retry window decides. (A live long attempt is
        # protected by the row-lock probe — see test_worker_holds_row.)
        row = _make_row(self.widget)
        TransitionMessage.objects.filter(pk=row.pk).update(
            modified=timezone.now() - timedelta(hours=2),
            started_at=timezone.now() - timedelta(hours=2),
            timeout_seconds=60,
        )

        with self.assertRaises(TransitionNotAllowed) as ctx:
            self.widget.process.cancel()
        self.assertNotIsInstance(
            ctx.exception, TransitionTemporarilyUnavailable)

    def test_retry_window_floor_keeps_short_retry_settings_transient(self):
        # These settings give RETRY_MINUTES=2 and MAX_ERRORS=3, so the computed
        # window is 8 minutes and the floor is 15. A 10-minute-old row sits
        # between the two, so only the floor keeps it transient.
        row = _make_row(self.widget)
        TransitionMessage.objects.filter(pk=row.pk).update(
            modified=timezone.now() - timedelta(minutes=10))

        with self.assertRaises(TransitionTemporarilyUnavailable):
            self.widget.process.cancel()

    @override_settings(DJANGO_LOGIC=dl_settings(
        TRANSITION_MESSAGE_MAX_ERRORS=2,
        TRANSITION_MESSAGE_RETRY_MINUTES=20,
    ))
    def test_retry_window_scales_with_the_retry_settings(self):
        # RETRY_MINUTES=20 and MAX_ERRORS=2 give a 60-minute window, so a
        # 30-minute-old row is still transient. A hardcoded 15 minutes fails
        # here.
        row = _make_row(self.widget)
        TransitionMessage.objects.filter(pk=row.pk).update(
            modified=timezone.now() - timedelta(minutes=30))

        with self.assertRaises(TransitionTemporarilyUnavailable):
            self.widget.process.cancel()

    def test_stranded_row_reclassifies_the_background_rejection_too(self):
        # Sending the same background transition to the queue again is the most
        # likely consumer retry, so enqueue shares the classification. It used
        # to answer AlreadyInProgress ("retry shortly") forever on a stranded
        # row.
        row = _make_row(self.widget, process_name='process')
        TransitionMessage.objects.filter(pk=row.pk).update(
            modified=timezone.now() - timedelta(hours=2))

        with self.assertRaises(TransitionNotAllowed) as ctx:
            with sync_execution():
                self.widget.process.fulfil()

        self.assertNotIsInstance(
            ctx.exception, TransitionTemporarilyUnavailable)
        self.assertIn('stranded', str(ctx.exception))
        # No second row was created, and the lock was released.
        self.assertEqual(TransitionMessage.objects.count(), 1)
        self.assertFalse(State(self.widget, 'status', 'process').is_locked())

    def test_running_row_rejects_a_second_enqueue_as_transient(self):
        # Control for the test above: a row that is still being retried keeps
        # answering AlreadyInProgress.
        _make_row(self.widget, process_name='process')

        with self.assertRaises(AlreadyInProgress):
            with sync_execution():
                self.widget.process.fulfil()

    def test_public_in_flight_probe_reads_the_row(self):
        # in_flight() is the documented probe for consumer API seams. One
        # shared queryset backs it, the synchronous gate, and the Action
        # failure path.
        self.assertFalse(in_flight(self.widget, 'process'))

        row = _make_row(self.widget)
        self.assertTrue(in_flight(self.widget, 'process'))
        # Default process_name is 'process'.
        self.assertTrue(in_flight(self.widget))
        # Scoped per process, and completed rows do not count.
        self.assertFalse(in_flight(self.widget, 'other_process'))
        TransitionMessage.objects.filter(pk=row.pk).update(is_completed=True)
        self.assertFalse(in_flight(self.widget, 'process'))

    def test_public_probe_answers_false_for_a_stranded_row(self):
        # The probe shapes 409 "retry shortly" answers, so it uses the same
        # rule as the gate: a stranded row is not busy, and calling it busy
        # would make the consumer retry forever.
        row = _make_row(self.widget)
        TransitionMessage.objects.filter(pk=row.pk).update(
            modified=timezone.now() - timedelta(hours=2))

        self.assertFalse(in_flight(self.widget, 'process'))

    def test_probe_failure_keeps_the_original_exception_and_runs_hooks(self):
        # The side-effect that failed may have left the connection needing a
        # rollback, so the probe itself raises TransactionManagementError. That
        # error must not replace the original one, and both failure hook
        # bundles must still run.
        widget = Widget.objects.create()
        hooks = []

        def insert_duplicate_then_fail(instance, **kwargs):
            # IntegrityError propagates and marks the transaction as needing a
            # rollback, without any signal receiver being involved.
            Widget.objects.create(pk=instance.pk)

        def record_fcb(instance, exception, **kwargs):
            hooks.append('failure_callbacks')

        action = Action(
            'poke_fail', sources=['draft'], failed_state='poke_failed',
            side_effects=[insert_duplicate_then_fail],
            failure_callbacks=[record_fcb],
        )
        state = State(widget, 'status', 'process')

        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            with self.assertRaises(IntegrityError):
                # SideEffects.execute routes the failure into fail_transition
                # itself before it re-raises.
                action.change_state(state)

        self.assertEqual(hooks, ['failure_callbacks'])
        self.assertFalse(state.is_locked())
        self.assertIn('could not probe', '\n'.join(logs.output))

    def test_failing_action_skips_failed_state_write_while_row_uncompleted(self):
        # The cache lock is free while the row waits for the worker, so the
        # Action acquires it. The uncompleted row still owns the state field:
        # writing failed_state over the in_progress_state would supersede the
        # background work, or be overwritten by its target write. The write is
        # skipped and the failure stays visible.
        _make_row(self.widget)

        def boom(instance, **kwargs):
            raise ValueError('boom')

        action = Action('poke_fail', sources=['draft'],
                        failed_state='poke_failed', side_effects=[boom])
        state = State(self.widget, 'status', 'process')

        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            with self.assertRaises(ValueError):
                # SideEffects.execute routes the failure into fail_transition
                # itself before it re-raises.
                action.change_state(state)

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')  # write skipped
        self.assertFalse(state.is_locked())            # no lock leaked
        self.assertIn(
            'uncompleted TransitionMessage owns the state field',
            '\n'.join(logs.output),
        )


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class BackgroundEnqueueMutexTests(TransactionTestCase):
    """Enqueue versus the synchronous lock and the uncompleted row.

    TransactionTestCase, because one test needs the partial unique constraint
    to raise a real IntegrityError inside the enqueue transaction.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.widget = Widget.objects.create()  # status='draft'

    def test_locked_state_rejects_background_transition_and_creates_no_row(self):
        # The reverse direction: a synchronous transition holds the cache lock,
        # so enqueue fails before it writes a row or the in_progress_state.
        state = State(self.widget, 'status', 'process')
        self.assertTrue(state.lock())
        self.addCleanup(state.unlock)

        with sync_execution():
            with self.assertRaises(TransitionNotAllowed) as ctx:
                self.widget.process.fulfil()

        self.assertEqual(str(ctx.exception), 'State is locked')
        self.assertEqual(TransitionMessage.objects.count(), 0)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')

        state.unlock()
        self.assertFalse(state.is_locked())

    def test_enqueue_releases_lock_when_rejected_as_already_in_progress(self):
        # The partial unique constraint rejects a second uncompleted row as
        # AlreadyInProgress, and the finally must still release the lock.
        _make_row(self.widget)

        with sync_execution():
            with self.assertRaises(AlreadyInProgress) as ctx:
                self.widget.process.fulfil()

        self.assertIn('already in progress', str(ctx.exception))
        state = State(self.widget, 'status', 'process')
        self.assertFalse(state.is_locked())
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')  # no in_progress write
        # Only the pre-existing row survives — the rejected attempt rolled
        # back.
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_rejection_is_catchable_as_temporarily_unavailable(self):
        # A consumer that imports only the core package can answer "busy,
        # retry shortly" without importing the background subpackage.
        _make_row(self.widget)

        with sync_execution():
            with self.assertRaises(TransitionTemporarilyUnavailable) as ctx:
                self.widget.process.fulfil()

        self.assertIsInstance(ctx.exception, AlreadyInProgress)

    def test_enqueue_releases_lock_on_success(self):
        # On the happy path the same finally releases the lock before dispatch,
        # so the worker runs unlocked and the instance ends up unlocked.
        with sync_execution():
            self.widget.process.fulfil()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilled')
        self.assertFalse(State(self.widget, 'status', 'process').is_locked())
        row = TransitionMessage.objects.get()
        self.assertTrue(row.is_completed)


class EnqueuePostInsertRecheckTests(TransactionTestCase):
    """Enqueue re-reads the persisted state after it inserts the row.

    On PostgreSQL the insert waits for a concurrent insert on the same unique
    index to finish — the other row leaves the partial unique index when
    is_completed flips. The waiting insert is then admitted seconds after its
    under-the-lock check, against an instance the finished attempt has already
    moved to its target state. Without the recheck the transition ran twice:
    the Heroku harness showed two callers both getting HTTP 200 and the work
    executed twice.
    """

    def setUp(self):
        self.widget = Widget.objects.create()  # draft

    def test_state_moved_during_insert_is_rejected_and_rolled_back(self):
        from unittest.mock import patch

        real_create = TransitionMessage.objects.create

        def create_then_state_moves(**kwargs):
            # Stand in for the waiting insert: by the time it returns, the
            # other attempt has finished and moved the instance to its target.
            row = real_create(**kwargs)
            Widget.objects.filter(pk=self.widget.pk).update(status='fulfilled')
            return row

        with patch.object(TransitionMessage.objects, 'create',
                          side_effect=create_then_state_moves):
            with sync_execution():
                with self.assertRaises(TransitionNotAllowed) as ctx:
                    self.widget.process.fulfil()

        self.assertIn('persisted state moved', str(ctx.exception))
        # The rejected attempt rolled its row back, never wrote the
        # in_progress_state, and released the lock. The simulated write here
        # happens inside the enqueue transaction, so the rollback reverts it to
        # 'draft'; across two real connections the other 'fulfilled' write
        # survives.
        self.assertEqual(TransitionMessage.objects.count(), 0)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')
        self.assertFalse(State(self.widget, 'status', 'process').is_locked())

    def test_recheck_rejection_is_catchable_as_temporarily_unavailable(self):
        # Same core-import contract as the AlreadyInProgress guard: the
        # recheck's refusal means "busy, retry shortly", not "forbidden".
        from unittest.mock import patch

        real_create = TransitionMessage.objects.create

        def create_then_state_moves(**kwargs):
            row = real_create(**kwargs)
            Widget.objects.filter(pk=self.widget.pk).update(status='fulfilled')
            return row

        with patch.object(TransitionMessage.objects, 'create',
                          side_effect=create_then_state_moves):
            with sync_execution():
                with self.assertRaises(TransitionTemporarilyUnavailable) as ctx:
                    self.widget.process.fulfil()

        self.assertIsInstance(ctx.exception, SourceStateChanged)

    def test_retry_from_in_progress_still_admitted(self):
        # The recovery path must keep working: an instance stranded in the
        # in_progress_state with no uncompleted row can be driven again from
        # that state, and it completes.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])

        with sync_execution():
            self.widget.process.fulfil()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilled')
