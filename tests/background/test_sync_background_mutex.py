"""D2 — sync/background mutual exclusion.

A synchronous ``Transition`` and a background transition on the same
instance + process must never interleave:

* While an uncompleted ``TransitionMessage`` exists (the durable
  in-flight marker for background work), ``Transition.change_state``
  raises ``TransitionNotAllowed`` from its under-the-lock
  ``_ensure_no_background_in_flight`` revalidation — and releases the
  cache lock on the way out.
* While the cache lock is held (a sync transition mid-flight),
  ``BackgroundTransition.change_state`` fails phase 1 with
  ``TransitionNotAllowed("State is locked")`` and creates no
  ``TransitionMessage`` row.
* ``BackgroundTransition.change_state`` holds the cache lock only for
  its critical section and ALWAYS unlocks in a finally — on rejection
  (``AlreadyInProgress``) and on success alike.
* Plain ``Action`` is documented as NOT gated on its success path: it does
  not change state, takes no lock, and ignores in-flight background work.
  Its FAILURE path's ``failed_state`` write is the exception — while an
  uncompleted row exists, phase 2 owns the state field, so the write is
  skipped (#185 review).
"""
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Action
from django_logic.background.dispatch import in_flight, sync_execution
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


def _make_tm(widget, process_name='process', is_completed=False):
    """An in-flight (or completed) TransitionMessage row, created directly —
    exactly what phase 1 leaves behind while phase 2 is pending."""
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
    """D2: the uncompleted TransitionMessage row gates sync transitions."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.widget = Widget.objects.create()  # status='draft'

    def test_uncompleted_tm_blocks_sync_transition_and_releases_lock(self):
        # D2 (a): with background work in flight on this instance+process,
        # the sync 'cancel' is rejected under the lock by
        # _ensure_no_background_in_flight — and the lock is released.
        _make_tm(self.widget)

        with self.assertRaises(TransitionNotAllowed) as ctx:
            self.widget.process.cancel()

        self.assertIn(
            'background transition is in progress', str(ctx.exception)
        )
        # The except branch in Transition.change_state must unlock before
        # re-raising — otherwise the instance would be stranded locked.
        state = State(self.widget, 'status', 'process')
        self.assertFalse(state.is_locked())
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')

    def test_uncompleted_tm_for_other_process_does_not_block(self):
        # D2 (b): the gate is scoped per process — an independent state
        # machine's in-flight row must not block this process.
        _make_tm(self.widget, process_name='other_process')

        self.widget.process.cancel()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'cancelled')

    def test_completed_tm_does_not_block(self):
        # D2 (c): only UNCOMPLETED rows are in-flight markers; finished
        # background work must not gate anything.
        _make_tm(self.widget, is_completed=True)

        self.widget.process.cancel()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'cancelled')

    def test_sync_action_is_not_gated_by_uncompleted_tm(self):
        # D2 (g): a plain Action does not change state, takes no lock and
        # is documented as NOT TM-gated — it runs fine while background
        # work is in flight on the same instance + process.
        _make_tm(self.widget)
        ran = []

        def poke_side_effect(instance, **kwargs):
            ran.append(instance.pk)

        action = Action('poke', sources=['draft'], side_effects=[poke_side_effect])
        action.change_state(State(self.widget, 'status', 'process'))

        self.assertEqual(ran, [self.widget.pk])
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')  # Actions never move state
        self.assertFalse(State(self.widget, 'status', 'process').is_locked())

    def test_tm_gate_raises_the_transient_type(self):
        # The gated condition clears when the flight completes, so it is
        # the motivating case for TransitionTemporarilyUnavailable (#191):
        # a generic handler must be able to answer 409/retry, not 400.
        _make_tm(self.widget)

        with self.assertRaises(TransitionTemporarilyUnavailable):
            self.widget.process.cancel()

    def test_stale_uncompleted_tm_is_not_transient(self):
        # A row untouched past the retry horizon is stranded, not busy
        # (#195): "retry shortly" would be wrong forever, and the WARNING
        # demotion would stop a stuck instance from paging. The plain base
        # keeps generic handlers refusing and hook logging at ERROR.
        tm = _make_tm(self.widget)
        TransitionMessage.objects.filter(pk=tm.pk).update(
            modified=timezone.now() - timedelta(hours=2))

        with self.assertRaises(TransitionNotAllowed) as ctx:
            self.widget.process.cancel()

        self.assertNotIsInstance(
            ctx.exception, TransitionTemporarilyUnavailable)
        self.assertIn('stranded', str(ctx.exception))
        # The refusal path must still release the lock.
        self.assertFalse(State(self.widget, 'status', 'process').is_locked())

    def test_public_in_flight_probe_reads_the_marker(self):
        # #197: the documented probe for consumer API seams. One shared
        # queryset (TransitionMessage.in_flight_for) backs this, the sync
        # gate, and the Action failure path.
        self.assertFalse(in_flight(self.widget, 'process'))

        tm = _make_tm(self.widget)
        self.assertTrue(in_flight(self.widget, 'process'))
        # Default process_name is 'process'.
        self.assertTrue(in_flight(self.widget))
        # Scoped per process, and completed rows do not count.
        self.assertFalse(in_flight(self.widget, 'other_process'))
        TransitionMessage.objects.filter(pk=tm.pk).update(is_completed=True)
        self.assertFalse(in_flight(self.widget, 'process'))

    def test_probe_failure_keeps_the_original_exception_and_runs_hooks(self):
        # #194: the side-effect that brought us to fail_transition may have
        # rollback-poisoned the connection, so the in-flight probe itself
        # raises TransactionManagementError. That error must not replace
        # the original one, and both failure hook bundles must still run.
        widget = Widget.objects.create()
        hooks = []

        def poison_then_fail(instance, **kwargs):
            # IntegrityError propagates AND marks needs_rollback — the
            # receiver-free version of the poisoned-connection shape.
            Widget.objects.create(pk=instance.pk)

        def record_fse(instance, exception, **kwargs):
            hooks.append('failure_side_effects')

        def record_fcb(instance, exception, **kwargs):
            hooks.append('failure_callbacks')

        action = Action(
            'poke_fail', sources=['draft'], failed_state='poke_failed',
            side_effects=[poison_then_fail],
            failure_side_effects=[record_fse],
            failure_callbacks=[record_fcb],
        )
        state = State(widget, 'status', 'process')

        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            with self.assertRaises(IntegrityError):
                # SideEffects.execute routes the failure into
                # fail_transition itself before re-raising.
                action.change_state(state)

        self.assertEqual(
            hooks, ['failure_side_effects', 'failure_callbacks'])
        self.assertFalse(state.is_locked())
        self.assertIn('could not probe', '\n'.join(logs.output))

    def test_failing_action_skips_failed_state_write_while_tm_in_flight(self):
        # D2 (h): the cache lock is free for the whole queued/phase-2 span,
        # so the Action's atomic acquire succeeds — but the uncompleted row
        # is the durable owner of the state field, and writing failed_state
        # over the in_progress_state would supersede the flight (or be
        # destroyed by its target write). The write is skipped; the failure
        # stays fully visible.
        _make_tm(self.widget)

        def boom(instance, **kwargs):
            raise ValueError('boom')

        action = Action('poke_fail', sources=['draft'],
                        failed_state='poke_failed', side_effects=[boom])
        state = State(self.widget, 'status', 'process')

        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            with self.assertRaises(ValueError):
                # SideEffects.execute routes the failure into
                # fail_transition itself before re-raising.
                action.change_state(state)

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')  # write skipped
        self.assertFalse(state.is_locked())            # no lock leaked
        self.assertIn(
            'uncompleted TransitionMessage owns the state field',
            '\n'.join(logs.output),
        )


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class BackgroundPhaseOneMutexTests(TransactionTestCase):
    """D2: phase 1 of a background transition vs the sync lock / TM guard.

    TransactionTestCase because (e) depends on the partial unique
    constraint firing a real IntegrityError inside phase 1's atomic block.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.widget = Widget.objects.create()  # status='draft'

    def test_locked_state_rejects_background_transition_and_creates_no_tm(self):
        # D2 (d): reverse direction — a sync transition mid-flight (cache
        # lock held) makes phase 1 fail fast, before any TransitionMessage
        # or in_progress_state write.
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

    def test_phase_one_releases_lock_when_rejected_as_already_in_progress(self):
        # D2 (e): the partial unique constraint rejects a second in-flight
        # row as AlreadyInProgress; the finally in
        # BackgroundTransition.change_state must still release the lock.
        _make_tm(self.widget)

        with sync_execution():
            with self.assertRaises(AlreadyInProgress) as ctx:
                self.widget.process.fulfil()

        self.assertIn('already in progress', str(ctx.exception))
        state = State(self.widget, 'status', 'process')
        self.assertFalse(state.is_locked())
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')  # no in_progress write
        # Only the pre-existing row survives — the rejected attempt's
        # atomic block rolled back.
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_rejection_is_catchable_as_temporarily_unavailable(self):
        # A consumer holding only the core import can answer "busy, retry
        # shortly" without importing the background subpackage (#191).
        _make_tm(self.widget)

        with sync_execution():
            with self.assertRaises(TransitionTemporarilyUnavailable) as ctx:
                self.widget.process.fulfil()

        self.assertIsInstance(ctx.exception, AlreadyInProgress)

    def test_phase_one_releases_lock_on_success(self):
        # D2 (f): on the happy path the lock is released by the same
        # finally before dispatch — phase 2 then runs unlocked and the
        # instance ends up unlocked too.
        with sync_execution():
            self.widget.process.fulfil()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilled')
        self.assertFalse(State(self.widget, 'status', 'process').is_locked())
        tm = TransitionMessage.objects.get()
        self.assertTrue(tm.is_completed)


class PhaseOnePostInsertRecheckTests(TransactionTestCase):
    """Phase 1 re-verifies the persisted state after the TM insert.

    On PostgreSQL the insert can block in a speculative-insert wait while a
    concurrent flight's phase 2 finishes (its row leaves the partial unique
    index when is_completed flips). Phase 1 is then admitted seconds after
    its under-the-lock revalidation, against an instance the finished
    flight already moved to its target state — without the recheck it
    silently re-ran the transition (observed live on the Heroku harness:
    two concurrent phase 1s, both HTTP 200, the work executed twice).
    """

    def setUp(self):
        self.widget = Widget.objects.create()  # draft

    def test_state_moved_during_insert_is_rejected_and_rolled_back(self):
        from unittest.mock import patch

        real_create = TransitionMessage.objects.create

        def create_then_state_moves(**kwargs):
            # Simulate the speculative-insert wait: by the time the insert
            # returns, the concurrent flight has completed and moved the
            # instance to its target state.
            tm = real_create(**kwargs)
            Widget.objects.filter(pk=self.widget.pk).update(status='fulfilled')
            return tm

        with patch.object(TransitionMessage.objects, 'create',
                          side_effect=create_then_state_moves):
            with sync_execution():
                with self.assertRaises(TransitionNotAllowed) as ctx:
                    self.widget.process.fulfil()

        self.assertIn('persisted state moved', str(ctx.exception))
        # The admitted-then-rejected attempt rolled back its row and never
        # wrote in_progress_state; the lock is released. (The simulated
        # external write happened inside phase 1's atomic block, so the
        # rollback reverts it to 'draft' here — in the real cross-connection
        # race the other flight's 'fulfilled' write survives untouched.)
        self.assertEqual(TransitionMessage.objects.count(), 0)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')
        self.assertFalse(State(self.widget, 'status', 'process').is_locked())

    def test_recheck_rejection_is_catchable_as_temporarily_unavailable(self):
        # Same core-import contract as the AlreadyInProgress guard (#191):
        # the recheck's refusal means "busy, retry shortly", not "forbidden".
        from unittest.mock import patch

        real_create = TransitionMessage.objects.create

        def create_then_state_moves(**kwargs):
            tm = real_create(**kwargs)
            Widget.objects.filter(pk=self.widget.pk).update(status='fulfilled')
            return tm

        with patch.object(TransitionMessage.objects, 'create',
                          side_effect=create_then_state_moves):
            with sync_execution():
                with self.assertRaises(TransitionTemporarilyUnavailable) as ctx:
                    self.widget.process.fulfil()

        self.assertIsInstance(ctx.exception, SourceStateChanged)

    def test_retry_from_in_progress_still_admitted(self):
        # The legitimate recovery path must keep working: instance stranded
        # in in_progress_state with NO uncompleted row (e.g. after an
        # unrestorable-row finalization) — re-triggering the transition from
        # in_progress_state is allowed and completes.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])

        with sync_execution():
            self.widget.process.fulfil()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilled')
