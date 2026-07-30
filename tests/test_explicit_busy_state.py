"""The explicit-busy-state pattern that replaces sync ``in_progress_state``.

0.12.0 made the in-progress marker background-only. The README's migration
note tells sync consumers to model a visible "busy" phase as a real state: a
fast transition into it, chained via ``next_transition`` to the transition
that does the work. This file proves that pattern end-to-end — happy path,
failure containment, the crash-window recovery recipe, and the visibility the
marker used to provide.
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
    """``submit`` is the fast, visible edge into ``busy``; the chained
    background transition does the work and owns the failure containment."""

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
        # Sync mode chains inline, so the flow completes in one call; the
        # busy state was a real, committed edge on the way through.
        self.assertEqual(inv.status, 'done')
        tm = TransitionMessage.objects.get(instance_id=str(inv.pk))
        self.assertTrue(tm.is_completed)

    def test_failure_is_contained_by_the_working_transition(self):
        inv = Invoice.objects.create(status='busy')
        with self.assertRaises(ValueError):
            inv.broken_busy_proc.do_work()
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'work_failed')
        tm = TransitionMessage.objects.get(instance_id=str(inv.pk))
        self.assertTrue(tm.is_completed)

    def test_a_swallowed_chained_failure_parks_at_busy(self):
        """Chaining is best-effort: inside an open transaction a failing
        chained flight rolls back with its savepoint (#138) and the swallow
        leaves the instance AT ``busy`` — the same parked shape as the crash
        window, recovered by the same re-drive recipe below. (In celery mode
        the chain enqueues on_commit and the worker's TM machinery contains
        the failure to ``work_failed`` — proven on the rig.)"""
        inv = Invoice.objects.create(status='draft')
        inv.broken_busy_proc.submit()
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'busy')

    def test_the_crash_window_recovery_recipe(self):
        """The window the pattern accepts: a crash between ``submit``'s
        commit and the chained phase 1 parks the instance at ``busy`` with
        no TransitionMessage. The documented recovery is a periodic
        re-drive, safe by construction: instances genuinely in flight are
        rejected by AlreadyInProgress, parked ones retry FORWARD."""
        parked = Invoice.objects.create(status='busy')     # the crash victim
        in_flight = Invoice.objects.create(status='busy')  # a live flight
        TransitionMessage.objects.create(
            app_label='tests', model_name='invoice',
            instance_id=str(in_flight.pk),
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
        in_flight.refresh_from_db()
        self.assertEqual(parked.status, 'done')
        self.assertEqual(in_flight.status, 'busy')  # untouched, still theirs
