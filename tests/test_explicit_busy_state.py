"""A background submission saves its busy state and job together."""
from django.core.cache import cache
from django.db.models.signals import post_save
from django.test import TestCase, override_settings

from django_logic import Process, ProcessManager
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from tests import dl_settings, unbind_model_process
from tests.models import Invoice


def _work(instance, **kwargs):
    pass


def _broken_work(instance, **kwargs):
    raise ValueError('work failed')


class ExplicitBusyProcess(Process):
    process_name = 'explicit_busy_proc'
    transitions = [
        BackgroundTransition(
            'submit', sources=['draft'], target='done',
            in_progress_state='busy', failed_state='work_failed',
            side_effects=[_work],
        ),
    ]


class BrokenBusyProcess(Process):
    process_name = 'broken_busy_proc'
    transitions = [
        BackgroundTransition(
            'submit', sources=['draft'], target='done',
            in_progress_state='busy', failed_state='work_failed',
            side_effects=[_broken_work],
        ),
    ]


@override_settings(DJANGO_LOGIC={'BACKGROUND_EXECUTION': 'pull'})
class BackgroundBusyStateTests(TestCase):
    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, ExplicitBusyProcess, state_field='status')
        ProcessManager.bind_model_process(
            Invoice, BrokenBusyProcess, state_field='status')
        self.addCleanup(unbind_model_process, Invoice, ExplicitBusyProcess)
        self.addCleanup(unbind_model_process, Invoice, BrokenBusyProcess)
        cache.clear()
        self.addCleanup(cache.clear)

    def test_submission_saves_busy_state_with_its_unfinished_job(self):
        invoice = Invoice.objects.create(status='draft')
        invoice.explicit_busy_proc.submit()

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'busy')
        row = TransitionMessage.objects.get(instance_id=str(invoice.pk))
        self.assertEqual(row.transition_name, 'submit')
        self.assertFalse(row.is_completed)

    def test_failed_busy_state_save_discards_the_job_and_state_change(self):
        def reject_busy_save(sender, instance, **kwargs):
            if instance.status == 'busy':
                raise ValueError('busy state could not be saved')

        invoice = Invoice.objects.create(status='draft')
        post_save.connect(reject_busy_save, sender=Invoice)
        self.addCleanup(post_save.disconnect, reject_busy_save, sender=Invoice)
        with self.assertRaisesMessage(ValueError, 'busy state could not be saved'):
            invoice.explicit_busy_proc.submit()

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'draft')
        self.assertFalse(TransitionMessage.objects.exists())
        self.assertFalse(invoice.explicit_busy_proc.state.is_locked())

    @override_settings(DJANGO_LOGIC=dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=1))
    def test_inline_execution_completes_the_saved_job(self):
        invoice = Invoice.objects.create(status='draft')
        invoice.explicit_busy_proc.submit()

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'done')
        self.assertTrue(TransitionMessage.objects.get().is_completed)

    @override_settings(DJANGO_LOGIC=dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=1))
    def test_failed_work_records_the_failure_on_its_saved_job(self):
        invoice = Invoice.objects.create(status='draft')
        with self.assertRaisesMessage(ValueError, 'work failed'):
            invoice.broken_busy_proc.submit()

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'work_failed')
        row = TransitionMessage.objects.get()
        self.assertTrue(row.is_completed)
        self.assertTrue(row.ended_in_failure)
