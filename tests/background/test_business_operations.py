"""Interactive and automatic requests share ordinary business actions."""
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import DatabaseTransition, Process, ProcessManager
from django_logic.background import BackgroundTransition, Defer, PermanentFailure
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from tests import dl_settings, unbind_model_process
from tests.background.models import Widget
from tests.stability.base import requires_postgres


class DocumentRefused(ValueError):
    pass


def record_approval(instance, refuse=False, **kwargs):
    if refuse:
        raise DocumentRefused('The document does not meet the approval rules.')
    instance.se_log += 'approved,'
    instance.save(update_fields=['se_log'])


class DocumentProcess(Process):
    process_name = 'document'
    transitions = [
        DatabaseTransition('begin_review', sources=['draft'], target='reviewing'),
        DatabaseTransition(
            'approve', sources=['reviewing'], target='approved', failed_state='failed',
            atomic=True, side_effects=[record_approval],
        ),
        DatabaseTransition('cancel_review', sources=['reviewing'], target='cancelled'),
    ]


@contextmanager
def begin_review(transition, state, kwargs, *, using):
    state.instance.status = 'reviewing'
    state.instance.save(update_fields=['status'], using=using)
    yield


def review_document(instance, refuse=False, **kwargs):
    if instance.status == 'approved':
        return
    if instance.status == 'failed':
        raise DocumentRefused('The document does not meet the approval rules.')
    instance.document.approve(refuse=refuse)


class DocumentReviewProcess(Process):
    process_name = 'document_review'
    transitions = [
        BackgroundTransition(
            'review', sources=['draft', 'reviewing', 'approved', 'failed'],
            queue='interactive-review', atomic=False, no_retry_on=(DocumentRefused,),
            submission_context=begin_review, side_effects=[review_document],
        ),
        BackgroundTransition(
            'review_automatically', sources=['draft', 'reviewing', 'approved', 'failed'],
            queue='automatic-review', atomic=False, no_retry_on=(DocumentRefused,),
            submission_context=begin_review, side_effects=[review_document],
        ),
    ]


@contextmanager
def submit_document_reviews(transition, state, kwargs, *, using):
    for document in Widget.objects.filter(pk__in=kwargs['document_ids']).order_by('pk'):
        document.document_review.review()
    yield


def check_document_reviews(instance, document_ids, **kwargs):
    statuses = list(Widget.objects.filter(pk__in=document_ids).values_list('status', flat=True))
    if 'failed' in statuses:
        raise PermanentFailure('A document needs attention.')
    if any(status != 'approved' for status in statuses):
        raise Defer(until=timezone.now() + timedelta(seconds=1), reason='Waiting for document reviews.')


class BatchReviewProcess(Process):
    process_name = 'batch_review'
    transitions = [
        BackgroundTransition(
            'review_documents', sources=['draft'], in_progress_state='reviewing',
            target='approved', failed_state='failed', queue='batch-review',
            submission_context=submit_document_reviews, side_effects=[check_document_reviews],
        ),
    ]


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class BusinessOperationTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for process in [DocumentProcess, DocumentReviewProcess, BatchReviewProcess]:
            ProcessManager.bind_model_process(Widget, process, state_field='status')
            self.addCleanup(unbind_model_process, Widget, process)

    def test_interactive_and_automatic_queues_run_the_same_business_action(self):
        interactive = Widget.objects.create()
        automatic = Widget.objects.create()
        interactive.document_review.review()
        automatic.document_review.review_automatically()
        first = TransitionMessage.for_instance(interactive, 'document_review').get()
        second = TransitionMessage.for_instance(automatic, 'document_review').get()
        self.assertEqual(first.queue_name, 'interactive-review')
        self.assertEqual(second.queue_name, 'automatic-review')
        for instance, job in [(interactive, first), (automatic, second)]:
            instance.refresh_from_db()
            self.assertEqual(instance.status, 'reviewing')
            run_background_transition(job.pk)
            instance.refresh_from_db()
            job.refresh_from_db()
            self.assertEqual((instance.status, instance.se_log), ('approved', 'approved,'))
            self.assertTrue(job.is_completed)
            self.assertFalse(job.ended_in_failure)
            self.assertEqual(job.completion_kind, 'succeeded')

    def test_failed_job_submission_rolls_back_the_business_busy_state(self):
        document = Widget.objects.create()
        save = TransitionMessage.save

        def refuse_job(message, *args, **kwargs):
            save(message, *args, **kwargs)
            raise RuntimeError('The review request could not be saved.')

        with patch.object(TransitionMessage, 'save', refuse_job), self.assertRaises(RuntimeError):
            document.document_review.review()
        document.refresh_from_db()
        self.assertEqual(document.status, 'draft')
        self.assertFalse(TransitionMessage.objects.exists())

    def test_final_job_write_failure_does_not_repeat_committed_approval(self):
        document = Widget.objects.create()
        document.document_review.review()
        job = TransitionMessage.objects.get()
        complete = TransitionMessage.mark_as_completed

        def refuse_completion(message, *args, **kwargs):
            complete(message, *args, **kwargs)
            raise RuntimeError('The review result could not be saved.')

        with patch.object(TransitionMessage, 'mark_as_completed', refuse_completion):
            run_background_transition(job.pk)
        document.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual((document.status, document.se_log), ('approved', 'approved,'))
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 1)
        run_background_transition(job.pk)
        document.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual((document.status, document.se_log), ('approved', 'approved,'))
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)

    def test_immediate_and_queued_business_refusal_keep_the_failed_state(self):
        immediate = Widget.objects.create()
        immediate.document.begin_review()
        with self.assertRaises(DocumentRefused):
            immediate.document.approve(refuse=True)
        immediate.refresh_from_db()
        self.assertEqual((immediate.status, immediate.se_log), ('failed', ''))

        queued = Widget.objects.create()
        queued.document_review.review_automatically(refuse=True)
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        queued.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual((queued.status, queued.se_log), ('failed', ''))
        self.assertTrue(job.is_completed)
        self.assertTrue(job.ended_in_failure)
        self.assertEqual(job.last_error_message, 'The document does not meet the approval rules.')

    def test_cancelled_review_does_not_change_the_document(self):
        document = Widget.objects.create()
        document.document_review.review()
        job = TransitionMessage.objects.get()
        document.document.cancel_review()

        run_background_transition(job.pk)

        document.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual((document.status, document.se_log), ('cancelled', ''))
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)

    def test_batch_job_waits_for_all_saved_document_reviews(self):
        batch, first, second = (Widget.objects.create() for _ in range(3))
        batch.batch_review.review_documents(document_ids=[first.pk, second.pk])
        parent = TransitionMessage.for_instance(batch, 'batch_review').get()
        first_job = TransitionMessage.for_instance(first, 'document_review').get()
        second_job = TransitionMessage.for_instance(second, 'document_review').get()

        run_background_transition(parent.pk)
        parent.refresh_from_db()
        self.assertFalse(parent.is_completed)
        self.assertEqual(parent.deferral_count, 1)
        run_background_transition(first_job.pk)
        TransitionMessage.objects.filter(pk=parent.pk).update(eligible_at=timezone.now() - timedelta(seconds=1))
        run_background_transition(parent.pk)
        parent.refresh_from_db()
        self.assertFalse(parent.is_completed)
        self.assertEqual(parent.deferral_count, 2)

        run_background_transition(second_job.pk)
        TransitionMessage.objects.filter(pk=parent.pk).update(eligible_at=timezone.now() - timedelta(seconds=1))
        run_background_transition(parent.pk)
        batch.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual(batch.status, 'approved')
        self.assertTrue(parent.is_completed)
        self.assertFalse(parent.ended_in_failure)
        self.assertEqual(TransitionMessage.objects.count(), 3)
        self.assertFalse(TransitionMessage.objects.filter(is_completed=False).exists())

    def test_failed_batch_submission_does_not_leave_document_jobs_or_busy_states(self):
        batch, first, second = (Widget.objects.create() for _ in range(3))
        save = TransitionMessage.save

        def refuse_batch(message, *args, **kwargs):
            result = save(message, *args, **kwargs)
            if message.process_name == 'batch_review':
                raise RuntimeError('The batch request could not be saved.')
            return result

        with patch.object(TransitionMessage, 'save', refuse_batch), self.assertRaises(RuntimeError):
            batch.batch_review.review_documents(document_ids=[first.pk, second.pk])
        self.assertFalse(TransitionMessage.objects.exists())
        self.assertEqual(list(Widget.objects.values_list('status', flat=True)), ['draft', 'draft', 'draft'])
