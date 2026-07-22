"""recover_stranded_states — the fifth safety-net task (#136).

A hard-killed synchronous transition leaves its instance parked in the
transition's ``in_progress_state``: the lock self-expires and the
implicit-source rule keeps it re-drivable, but nothing ACTS — no failure
hooks, no alert. These tests pin the sweep's contract: provably-stranded
instances (in-progress + unlocked + no uncompleted TransitionMessage) are
driven through the owning transition's failure path; everything else is
left alone.
"""
from django.test import TestCase

from django_logic.background.dispatch import recover_stranded_states
from django_logic.background.models import TransitionMessage
from django_logic.background.settings import beat_schedule
from django_logic.process import Process, ProcessManager
from django_logic.state import State
from django_logic.transition import Transition
from tests.models import Invoice

FAILURE_LOG = []


def record_failure_side_effect(instance, **kwargs):
    FAILURE_LOG.append(('side_effect', instance.pk, str(kwargs.get('exception') or '')))


def record_failure_callback(instance, **kwargs):
    FAILURE_LOG.append(('callback', instance.pk))


class _StrandedProcess(Process):
    process_name = 'stranded_process'
    transitions = [
        Transition('sync_up', sources=['draft'], target='done',
                   in_progress_state='syncing',
                   failed_state='failed',
                   failure_side_effects=[record_failure_side_effect],
                   failure_callbacks=[record_failure_callback]),
        Transition('archive', sources=['done'], target='archived',
                   in_progress_state='archiving'),  # no failed_state
    ]


class RecoverStrandedStatesTests(TestCase):
    def setUp(self):
        super().setUp()
        FAILURE_LOG.clear()
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

    def test_healthy_instances_are_untouched(self):
        Invoice.objects.create(status='draft')
        Invoice.objects.create(status='done')
        self.assertEqual(recover_stranded_states(), 0)
        self.assertEqual(FAILURE_LOG, [])

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
