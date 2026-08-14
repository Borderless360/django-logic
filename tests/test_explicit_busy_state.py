"""The explicit busy state that replaces a synchronous ``in_progress_state``.

``in_progress_state`` is background-only, so a synchronous consumer models a
visible "busy" phase as a real state: a fast transition into it, chained by
``next_transition`` to the transition that does the work. These tests cover the
happy path, failure containment, and the recovery recipe for the crash window.
"""
from django.core.cache import cache
from django.test import TestCase, override_settings

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from tests.models import Invoice

_SYNC = {'BACKGROUND_EXECUTION': 'sync'}


def _work(instance, **kwargs):
    pass


def _broken_work(instance, **kwargs):
    raise ValueError('work failed')


class ExplicitBusyProcess(Process):
    """``submit`` is the fast, visible step into ``busy``. The chained
    background transition does the work and contains its own failure."""

    process_name = 'explicit_busy_proc'
    transitions = [
        Transition(
            'submit', sources=['draft'], target='busy',
            next_transition='do_work',
        ),
        BackgroundTransition(
            'do_work', sources=['busy'], target='done',
            failed_state='work_failed', side_effects=[_work],
        ),
    ]


class BrokenBusyProcess(Process):
    process_name = 'broken_busy_proc'
    transitions = [
        Transition(
            'submit', sources=['draft'], target='busy',
            next_transition='do_work',
        ),
        BackgroundTransition(
            'do_work', sources=['busy'], target='done',
            failed_state='work_failed', side_effects=[_broken_work],
        ),
    ]


@override_settings(DJANGO_LOGIC={**_SYNC, 'TRANSITION_MESSAGE_MAX_ERRORS': 1})
class ExplicitBusyStatePatternTests(TestCase):
    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, ExplicitBusyProcess, state_field='status')
        ProcessManager.bind_model_process(
            Invoice, BrokenBusyProcess, state_field='status')
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, ExplicitBusyProcess)
        self.addCleanup(
            ProcessManager.unbind_model_process, Invoice, BrokenBusyProcess)
        cache.clear()
        self.addCleanup(cache.clear)

    def test_happy_path_shows_busy_then_completes(self):
        inv = Invoice.objects.create(status='draft')
        inv.explicit_busy_proc.submit()
        inv.refresh_from_db()
        # Sync mode chains inline, so the flow finishes in one call. The busy
        # state was a real committed state on the way through.
        self.assertEqual(inv.status, 'done')
        row = TransitionMessage.objects.get(instance_id=str(inv.pk))
        self.assertTrue(row.is_completed)

    def test_failure_is_contained_by_the_working_transition(self):
        inv = Invoice.objects.create(status='busy')
        with self.assertRaises(ValueError):
            inv.broken_busy_proc.do_work()
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'work_failed')
        row = TransitionMessage.objects.get(instance_id=str(inv.pk))
        self.assertTrue(row.is_completed)

    def test_a_swallowed_chained_failure_parks_at_busy(self):
        """Chaining is best-effort. Inside an open transaction the chained
        transition rolls back with its savepoint and the swallowed error leaves
        the instance at ``busy`` — the same parked shape as the crash window
        below, with the same recovery. In celery mode the chain enqueues on
        commit and the worker contains the failure in ``work_failed``."""
        inv = Invoice.objects.create(status='draft')
        inv.broken_busy_proc.submit()
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'busy')

    def test_the_crash_window_recovery_recipe(self):
        """The pattern accepts one window: a crash between ``submit``'s commit
        and the chained enqueue parks the instance at ``busy`` with no
        TransitionMessage. The recovery is a periodic job that drives
        ``do_work`` again — an instance that is really in progress is refused
        with AlreadyInProgress, and a parked one moves forward."""
        parked = Invoice.objects.create(status='busy')    # parked by the crash
        running = Invoice.objects.create(status='busy')   # really in progress
        TransitionMessage.objects.create(
            app_label='tests', model_name='invoice',
            instance_id=str(running.pk),
            process_name='explicit_busy_proc', transition_name='do_work',
            queue_name='django_logic',
        )

        recovered, skipped = 0, 0
        for inv in Invoice.objects.filter(status='busy'):
            try:
                inv.explicit_busy_proc.do_work()
                recovered += 1
            except AlreadyInProgress:
                skipped += 1

        self.assertEqual((recovered, skipped), (1, 1))
        parked.refresh_from_db()
        running.refresh_from_db()
        self.assertEqual(parked.status, 'done')
        self.assertEqual(running.status, 'busy')  # untouched
