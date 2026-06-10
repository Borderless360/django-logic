"""Regressions for GitHub issues #85, #87, #90, #91 (engine level).

#85 — the cache lock must be released on EVERY failure path after
acquisition: a failed ``in_progress_state`` write, a failed target write in
``complete_transition``, and a failed ``failed_state`` write in
``fail_transition``. Pre-fix, any of those froze the instance's FSM for the
full ``LOCK_TIMEOUT`` (every transition raised "State is locked").

#87 — positional arguments to a process transition method used to be
silently dropped; ``instance.process.verify(user)`` ran with ``user=None``
and therefore bypassed all permission checks. Now a ``TypeError``.

#90 — the background runner reloads instances with ``_base_manager``, so a
filtered default manager (archived rows hidden) cannot strand an in-flight
transition.

#91 — every django-logic Celery task pairs ``acks_late=True`` with
``reject_on_worker_lost=True`` at the task level, so crash re-delivery does
not depend on the consumer's global Celery configuration.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from django_logic import Transition
from django_logic.state import State
from tests.background.models import ArchivableWidget, Widget
from tests.models import Invoice


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


def _boom_set_state(self, value):
    raise RuntimeError(f'simulated DB failure writing {value!r}')


class LockReleasedOnWriteFailureTests(TestCase):
    """#85 — no state-write failure may leak the lock."""

    def setUp(self):
        self.invoice = Invoice.objects.create(status='draft')
        self.state = State(self.invoice, 'status', 'process')

    def test_failed_in_progress_write_releases_the_lock(self):
        transition = Transition(
            'go', sources=['draft'], target='done', in_progress_state='doing'
        )
        with patch.object(State, 'set_state', _boom_set_state):
            with self.assertRaises(RuntimeError):
                transition.change_state(self.state)
        self.assertFalse(self.state.is_locked(),
                         'in_progress write failure leaked the lock')

    def test_failed_target_write_releases_the_lock(self):
        transition = Transition('go', sources=['draft'], target='done')
        with patch.object(State, 'set_state', _boom_set_state):
            with self.assertRaises(RuntimeError):
                transition.change_state(self.state)
        self.assertFalse(self.state.is_locked(),
                         'target write failure leaked the lock')

    def test_failed_failed_state_write_releases_the_lock(self):
        def explode(instance, **kwargs):
            raise ValueError('side effect failed')

        transition = Transition(
            'go', sources=['draft'], target='done',
            failed_state='failed', side_effects=[explode],
        )
        with patch.object(State, 'set_state', _boom_set_state):
            with self.assertRaises(Exception):
                transition.change_state(self.state)
        self.assertFalse(self.state.is_locked(),
                         'failed_state write failure leaked the lock')

    def test_instance_is_usable_again_after_a_failed_write(self):
        # The point of #85: the NEXT transition must not be rejected with
        # "State is locked".
        transition = Transition('go', sources=['draft'], target='done')
        with patch.object(State, 'set_state', _boom_set_state):
            with self.assertRaises(RuntimeError):
                transition.change_state(self.state)
        transition.change_state(self.state)  # unpatched: succeeds
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'done')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class PositionalArgumentsRejectedTests(TestCase):
    """#87 — positional args raise instead of silently dropping user."""

    def test_positional_argument_raises_type_error(self):
        widget = Widget.objects.create()
        with self.assertRaises(TypeError) as ctx:
            widget.process.cancel(object())
        self.assertIn('keyword arguments only', str(ctx.exception))

    def test_keyword_call_still_works(self):
        widget = Widget.objects.create()
        widget.process.cancel(user=None)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'cancelled')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class BaseManagerRestoreTests(TestCase):
    """#90 — a filtered default manager cannot strand in-flight work."""

    def test_archived_instance_is_still_restored_and_completed(self):
        from django_logic.background.models import TransitionMessage
        from django_logic.background.runner import run_background_transition

        widget = ArchivableWidget.objects.create()
        # Phase 1 happened: in_progress + TM row.
        widget.status = 'finishing'
        widget.save(update_fields=['status'])
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='archivablewidget',
            instance_id=str(widget.pk),
            process_name='process',
            field_name='status',
            transition_name='finish',
            queue_name='django_logic.critical',
        )
        # The instance is archived between phase 1 and phase 2 — it vanishes
        # from the default manager but still exists.
        ArchivableWidget.all_objects.filter(pk=widget.pk).update(archived=True)
        self.assertFalse(ArchivableWidget.objects.filter(pk=widget.pk).exists())

        # Pre-fix: DoesNotExist -> _RestoreError -> row completed with the
        # instance stranded in 'finishing' forever.
        run_background_transition(tm.pk)

        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        fresh = ArchivableWidget.all_objects.get(pk=widget.pk)
        self.assertEqual(fresh.status, 'done')

    def test_get_persisted_state_reads_through_filtered_manager(self):
        widget = ArchivableWidget.all_objects.create(archived=True, status='x')
        state = State(widget, 'status', 'process')
        self.assertEqual(state.get_persisted_state(), 'x')


class TaskCrashRedeliveryConfigTests(TestCase):
    """#91 — acks_late is paired with reject_on_worker_lost per task."""

    def test_all_tasks_pair_acks_late_with_reject_on_worker_lost(self):
        from django_logic.background import tasks

        for task in (
            tasks.run_background_transition_task,
            tasks.retry_stale_transitions,
            tasks.cleanup_completed_transitions,
            tasks.detect_stuck_transitions,
            tasks.watchdog_stale_attempts,
        ):
            self.assertTrue(task.acks_late, task.name)
            self.assertTrue(task.reject_on_worker_lost, task.name)
