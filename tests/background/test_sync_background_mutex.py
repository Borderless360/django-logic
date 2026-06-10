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
* Plain ``Action`` is documented as NOT gated: it does not change state,
  takes no lock, and ignores in-flight background work.
"""
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings

from django_logic import Action
from django_logic.background.dispatch import sync_execution
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State
from tests.background.models import Widget


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


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
