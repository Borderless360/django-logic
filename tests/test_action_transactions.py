from datetime import timedelta

from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process, Transition
from django_logic.background import BackgroundTransition, PermanentFailure
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import claim_next
from django_logic.background.runner import run_background_transition, run_claimed_transition
from django_logic.exceptions import RefusalReason, TransitionNotAllowed
from tests import dl_settings
from tests.models import Invoice
from tests.stability.base import requires_postgres


def available(invoice):
    return invoice.is_available


def received(invoice, user):
    return invoice.customer_received


def preserve_input(invoice, **kwargs):
    assert invoice.customer_received


def review_result(invoice, fail=False, **kwargs):
    Review(field_name='status', instance=invoice).accept()
    if fail:
        raise PermanentFailure('review failed')


def note_completion(invoice, **kwargs):
    Invoice.objects.filter(pk=invoice.pk).update(customer_received=True)


class Review(Process):
    process_name = 'review'
    transitions = [
        Transition('accept', sources=['running'], target='accepted'),
        Transition('submit', sources=['draft'], target='accepted', conditions=[available], side_effects=[preserve_input]),
        BackgroundTransition('review', sources=['draft'], in_progress_state='running', target='done',
                             conditions=[available], side_effects=[review_result]),
        BackgroundTransition('send', sources=['draft'], in_progress_state='running', target='done',
                             permissions=[received], side_effects=[review_result]),
        BackgroundTransition('upload', sources=['draft'], in_progress_state='running', target='done',
                             failed_state='failed', atomic=False, side_effects=[review_result]),
        Transition('finish', sources=['draft'], target='done', callbacks=[note_completion]),
    ]


class GuardTransactionTests(TestCase):
    def setUp(self):
        self.invoice = Invoice.objects.create(status='draft', is_available=True, customer_received=True)
        self.process = Review(field_name='status', instance=self.invoice)

    def test_changed_condition_refuses_sync_and_background_actions(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(is_available=False)
        for action in [self.process.submit, self.process.review]:
            with self.assertRaises(TransitionNotAllowed) as refusal:
                action()
            self.assertEqual(refusal.exception.reason, RefusalReason.CONDITION)
        self.assertFalse(TransitionMessage.objects.exists())
        self.assertEqual(self.invoice.status, 'draft')

    def test_changed_permission_refuses_submission(self):
        from django.contrib.auth import get_user_model
        Invoice.objects.filter(pk=self.invoice.pk).update(customer_received=False)
        with self.assertRaises(TransitionNotAllowed) as refusal:
            self.process.send(user=get_user_model().objects.create(username='reviewer'))
        self.assertEqual(refusal.exception.reason, RefusalReason.PERMISSION)
        self.assertFalse(TransitionMessage.objects.exists())

    def test_guards_see_database_record_and_required_work_keeps_caller_input(self):
        self.invoice.is_available = False
        Invoice.objects.filter(pk=self.invoice.pk).update(customer_received=False)
        self.process.submit()
        self.assertEqual(self.invoice.status, 'accepted')

    def test_another_declaration_matching_after_reload_refuses_the_action(self):
        class Route(Process):
            transitions = [
                Transition('choose', ['draft'], target='first', conditions=[available]),
                Transition('choose', ['draft'], target='second', conditions=[lambda obj: not available(obj)]),
            ]
        Invoice.objects.filter(pk=self.invoice.pk).update(is_available=False)
        with self.assertRaises(TransitionNotAllowed) as refused:
            Route(field_name='status', instance=self.invoice).choose()
        self.assertEqual(refused.exception.reason, RefusalReason.CONDITION)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')


@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class ActionCommitTests(TransactionTestCase):
    def setUp(self):
        self.invoice = Invoice.objects.create(status='draft')
        self.process = Review(field_name='status', instance=self.invoice)

    def test_callbacks_wait_for_outer_commit_and_disappear_on_rollback(self):
        with self.assertRaises(ValueError):
            with transaction.atomic():
                self.process.finish()
                self.invoice.refresh_from_db()
                self.assertFalse(self.invoice.customer_received)
                raise ValueError
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(self.invoice.customer_received)
        with transaction.atomic():
            self.process.finish()
            self.invoice.refresh_from_db()
            self.assertFalse(self.invoice.customer_received)
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.customer_received)

    def test_an_external_call_is_refused_and_the_running_job_can_choose_its_result(self):
        self.process.review()
        with self.assertRaises(TransitionNotAllowed) as refusal:
            self.process.accept()
        self.assertEqual(refusal.exception.reason, RefusalReason.BACKGROUND_IN_FLIGHT)
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        self.assertEqual(self.invoice.status, 'accepted')

    @requires_postgres
    def test_non_atomic_attempt_preserves_its_chosen_result_on_failure(self):
        self.process.upload(fail=True)
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertTrue(job.ended_in_failure)
        self.assertEqual(self.invoice.status, 'accepted')

    @requires_postgres
    def test_other_workers_skip_a_claim_and_can_recover_a_stopped_process(self):
        self.process.review()
        job = TransitionMessage.objects.get()
        self.assertEqual(claim_next([job.queue_name]), job.pk)
        self.assertIsNone(claim_next([job.queue_name]))
        job.refresh_from_db()
        self.assertEqual(job.start_attempt_count, 1)
        first = job.first_started_at
        TransitionMessage.objects.filter(pk=job.pk).update(started_at=timezone.now() - timedelta(seconds=6))
        self.assertEqual(claim_next([job.queue_name]), job.pk)
        run_claimed_transition(job.pk)
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertEqual(job.start_attempt_count, 2)
        self.assertEqual(job.first_started_at, first)
