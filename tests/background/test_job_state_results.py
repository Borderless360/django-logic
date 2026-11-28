"""A worker's explicit choice follows the business transaction, including savepoints and retry."""
import os
from datetime import timedelta

from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.models import JobStateResult, TransitionMessage
from django_logic.background.pull import run_once
from django_logic.background.runner import run_background_transition
from django_logic.background.safety_nets import cleanup_completed_transitions
from tests import dl_settings
from tests.models import Invoice
from tests.stability.base import requires_postgres


def choose_then_rollback(invoice, keep_first=False, **kwargs):
    review = Review(instance=invoice, field_name='status')
    if keep_first:
        review.approve()
    with transaction.atomic():
        if keep_first:
            review.reject()
        else:
            review.keep_running()
        transaction.set_rollback(True)


def submit_with_receipt(invoice, stop=False, **kwargs):
    if not invoice.customer_received:
        # Commit the receipt together with the state that the worker selected.
        with transaction.atomic():
            Review(instance=invoice, field_name='status').approve()
            invoice.customer_received = True
            invoice.save(update_fields=['customer_received'])
        if stop:
            os._exit(3)
        raise ValueError('Report unavailable')
    invoice.is_available = True
    invoice.save(update_fields=['is_available'])


class Review(Process):
    process_name = 'order_review'
    transitions = [
        Transition('approve', ['running'], target='approved'),
        Transition('reject', ['approved'], target='rejected'),
        Transition('keep_running', ['running'], target='running'),
        BackgroundTransition('review', ['draft'], target='done', in_progress_state='running',
                             side_effects=[choose_then_rollback]),
        BackgroundTransition('submit', ['draft'], target='done', in_progress_state='running',
                             side_effects=[submit_with_receipt], atomic=False),
    ]


@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_RETRY_MINUTES=0))
class JobStateResultTests(TransactionTestCase):
    def setUp(self):
        self.invoice = Invoice.objects.create(status='draft', is_available=False)
        self.review = Review(instance=self.invoice, field_name='status')

    def assert_completion(self, job, state, completion):
        job.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertEqual(job.completion_kind, completion)
        self.assertEqual(self.invoice.status, state)

    def test_rolled_back_decision_does_not_replace_the_first_decision(self):
        self.review.review(keep_first=True)
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        self.assert_completion(job, 'approved', 'succeeded')

    def test_rolled_back_decision_matching_the_busy_state_leaves_the_declared_target(self):
        self.review.review()
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        self.assert_completion(job, 'done', 'succeeded')

    @requires_postgres
    def test_non_atomic_retry_completes_required_work_after_selecting_a_state(self):
        self.review.submit()
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 1)
        run_background_transition(job.pk)
        self.assert_completion(job, 'approved', 'succeeded')
        self.assertTrue(self.invoice.is_available)

    @requires_postgres
    def test_worker_process_death_keeps_the_committed_decision_for_its_replacement(self):
        self.review.submit(stop=True)
        job = TransitionMessage.objects.get()
        self.assertTrue(run_once([job.queue_name], isolate=True))
        job.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 1)
        self.assertTrue(run_once([job.queue_name], isolate=True))
        self.assert_completion(job, 'approved', 'succeeded')
        self.assertTrue(self.invoice.is_available)

    @requires_postgres
    def test_an_outside_change_after_the_decision_supersedes_the_retried_job(self):
        self.review.submit()
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        Invoice.objects.filter(pk=self.invoice.pk).update(status='cancelled')
        run_background_transition(job.pk)
        self.assert_completion(job, 'cancelled', 'superseded')
        self.assertFalse(self.invoice.is_available)

    def test_cleanup_removes_the_decision_and_counts_one_completed_job(self):
        self.review.review(keep_first=True)
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        self.assertTrue(JobStateResult.objects.filter(job=job).exists())
        TransitionMessage.objects.filter(pk=job.pk).update(modified=timezone.now() - timedelta(days=30))
        self.assertEqual(cleanup_completed_transitions(), 1)
        self.assertFalse(JobStateResult.objects.exists())

    def test_downgrade_waits_for_jobs_with_committed_decisions(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes('django_logic_background')

        def migrate_to(target):
            MigrationExecutor(connection).migrate([('django_logic_background', target)])

        def restore_latest():
            MigrationExecutor(connection).migrate(latest)

        self.addCleanup(restore_latest)
        self.review.review()
        job = TransitionMessage.objects.get()
        JobStateResult.objects.create(job=job, value='approved')
        with self.assertRaisesRegex(RuntimeError, 'Finish jobs with a saved state decision'):
            migrate_to('0023_remove_state_lock_ownership')
        self.assertTrue(JobStateResult.objects.filter(job=job, value='approved').exists())

        job.mark_as_completed()
        migrate_to('0023_remove_state_lock_ownership')
        self.assertNotIn(JobStateResult._meta.db_table, connection.introspection.table_names())
        self.assertTrue(TransitionMessage.objects.filter(pk=job.pk, is_completed=True).exists())
