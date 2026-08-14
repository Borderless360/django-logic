"""Four engine rules, each one written after a consumer hit the defect.

The cache lock is released on every failure path after it is taken: a failed
``in_progress_state`` write, a failed target write, and a failed
``failed_state`` write. Before the fix any of those froze the instance for the
whole ``LOCK_TIMEOUT``, and every later transition raised "State is locked".

A positional argument to a process transition method raises ``TypeError``. It
used to be dropped, so ``instance.process.verify(user)`` ran with
``user=None`` and skipped every permission check.

The background runner reloads the instance through ``_base_manager``, so a
default manager that hides archived rows cannot strand a running transition.

Every django-logic Celery task sets both ``acks_late=True`` and
``reject_on_worker_lost=True``, so crash re-delivery does not depend on the
consumer's global Celery configuration.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from django_logic import Transition
from django_logic.state import State
from tests.background.models import ArchivableWidget, Widget
from tests.models import Invoice
from tests import dl_settings


def _boom_set_state(self, value):
    raise RuntimeError(f'simulated DB failure writing {value!r}')


class LockReleasedOnWriteFailureTests(TestCase):
    """No state-write failure may leave the lock held."""

    def setUp(self):
        self.invoice = Invoice.objects.create(status='draft')
        self.state = State(self.invoice, 'status', 'process')

    def test_failed_target_write_releases_the_lock(self):
        transition = Transition('go', sources=['draft'], target='done')
        with patch.object(State, 'set_state', _boom_set_state):
            with self.assertRaises(RuntimeError):
                transition.change_state(self.state)
        self.assertFalse(self.state.is_locked(),
                         'the failed target write left the lock held')

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
                         'the failed failed_state write left the lock held')

    def test_instance_is_usable_again_after_a_failed_write(self):
        # The next transition must not be rejected with "State is locked".
        transition = Transition('go', sources=['draft'], target='done')
        with patch.object(State, 'set_state', _boom_set_state):
            with self.assertRaises(RuntimeError):
                transition.change_state(self.state)
        transition.change_state(self.state)  # no patch, so this one succeeds
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'done')


class PositionalArgumentsRejectedTests(TestCase):
    """A positional argument raises instead of dropping the user."""

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


class BaseManagerRestoreTests(TestCase):
    """A filtered default manager cannot strand a running transition."""

    def test_archived_instance_is_still_restored_and_completed(self):
        from django_logic.background.models import TransitionMessage
        from django_logic.background.runner import run_background_transition

        widget = ArchivableWidget.objects.create()
        # Enqueue already ran: the in-progress state and the row both exist.
        widget.status = 'finishing'
        widget.save(update_fields=['status'])
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='archivablewidget',
            instance_id=str(widget.pk),
            process_name='process',
            field_name='status',
            transition_name='finish',
            queue_name='django_logic.critical',
        )
        # The instance is archived between enqueue and execute. It disappears
        # from the default manager, but the row is still there.
        ArchivableWidget.all_objects.filter(pk=widget.pk).update(archived=True)
        self.assertFalse(ArchivableWidget.objects.filter(pk=widget.pk).exists())

        # Before the fix the reload failed, the row completed with no work
        # done, and the instance stayed stranded in 'finishing'.
        run_background_transition(transition_message.pk)

        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        fresh = ArchivableWidget.all_objects.get(pk=widget.pk)
        self.assertEqual(fresh.status, 'done')

    def test_get_persisted_state_reads_through_filtered_manager(self):
        widget = ArchivableWidget.all_objects.create(archived=True, status='x')
        state = State(widget, 'status', 'process')
        self.assertEqual(state.get_persisted_state(), 'x')


class TaskCrashRedeliveryConfigTests(TestCase):
    """Every task pairs acks_late with reject_on_worker_lost."""

    def test_all_tasks_pair_acks_late_with_reject_on_worker_lost(self):
        # The task list is read from the module, never written by hand. A
        # hardcoded list once stopped covering a new task, so both keywords
        # could be removed from it and the suite still passed.
        from django_logic.background import tasks

        found = [
            obj for obj in vars(tasks).values()
            if hasattr(obj, 'apply_async')
            and str(getattr(obj, 'name', '')).startswith('django_logic.')
        ]
        self.assertEqual(
            len(found), 5,
            'expected to find every shared task in '
            'django_logic.background.tasks; found %s' % sorted(t.name for t in found))
        for task in found:
            self.assertTrue(task.acks_late, task.name)
            self.assertTrue(task.reject_on_worker_lost, task.name)
