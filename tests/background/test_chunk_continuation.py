"""A saved import job keeps its progress between bounded page attempts."""
from datetime import timedelta
import os
from unittest.mock import patch

from django.core.cache import cache
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager
from django_logic.background import BackgroundTransition, Defer
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import run_once
from django_logic.background.runner import run_background_transition
from tests import dl_settings, unbind_model_process
from tests.background.models import Widget
from tests.stability.base import requires_postgres


def import_next_page(instance, stop_after_page=False, **kwargs):
    with transaction.atomic():
        current = Widget.objects.select_for_update().get(pk=instance.pk)
        if current.status != 'importing':
            return
        cursor = current.kwargs_seen['cursor']
        if cursor is None:
            return
        current.se_log += f'page:{cursor},'
        current.kwargs_seen = {'cursor': 1 if cursor == 0 else None}
        current.save(update_fields=['se_log', 'kwargs_seen'])
    if stop_after_page and cursor == 0:
        os._exit(17)
    if current.kwargs_seen['cursor'] is not None:
        raise Defer(until=timezone.now() + timedelta(seconds=1), reason='The import has another page.')


class ChunkImportProcess(Process):
    process_name = 'chunk_import'
    transitions = [
        BackgroundTransition(
            action_name='import_pages',
            sources=['importing'],
            target='imported',
            queue='chunk-import',
            atomic=False,
            side_effects=[import_next_page],
        ),
    ]


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class ChunkContinuationTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        ProcessManager.bind_model_process(Widget, ChunkImportProcess, state_field='status')
        self.addCleanup(unbind_model_process, Widget, ChunkImportProcess)
        self.widget = Widget.objects.create(status='importing', kwargs_seen={'cursor': 0})

    def assert_progress(self, cursor, log, status='importing'):
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.kwargs_seen, {'cursor': cursor})
        self.assertEqual(self.widget.se_log, log)
        self.assertEqual(self.widget.status, status)
        self.assertFalse(self.widget.chunk_import.state.is_locked())

    def resume(self, job):
        TransitionMessage.objects.filter(pk=job.pk).update(eligible_at=timezone.now() - timedelta(seconds=1))
        run_background_transition(job.pk)

    def test_page_failure_rolls_back_the_page_and_cursor(self):
        self.widget.chunk_import.import_pages()
        job = TransitionMessage.objects.get()
        save = Widget.save

        def fail_after_saving_page(instance, *args, **kwargs):
            result = save(instance, *args, **kwargs)
            if kwargs.get('update_fields') == ['se_log', 'kwargs_seen']:
                raise RuntimeError('The page could not finish.')
            return result

        with patch.object(Widget, 'save', fail_after_saving_page):
            run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 1)
        self.assert_progress(0, '')

        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.deferral_count, 1)
        self.assert_progress(1, 'page:0,')

        self.resume(job)
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        self.assert_progress(None, 'page:0,page:1,', status='imported')
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_completed_page_survives_a_stopped_job_process(self):
        self.widget.chunk_import.import_pages(stop_after_page=True)
        job = TransitionMessage.objects.get()

        run_once(['chunk-import'], isolate=True)

        job.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 1)
        self.assert_progress(1, 'page:0,')
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        self.assert_progress(None, 'page:0,page:1,', status='imported')
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_failed_final_completion_does_not_repeat_saved_pages(self):
        self.widget.chunk_import.import_pages()
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        complete = TransitionMessage.mark_as_completed

        def fail_after_completing(message, *args, **kwargs):
            complete(message, *args, **kwargs)
            raise RuntimeError('The final completion could not finish.')

        with patch.object(TransitionMessage, 'mark_as_completed', fail_after_completing):
            self.resume(job)
        job.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 1)
        self.assert_progress(None, 'page:0,page:1,')

        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        self.assert_progress(None, 'page:0,page:1,', status='imported')
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_waiting_job_leaves_cancelled_import_unchanged(self):
        self.widget.chunk_import.import_pages()
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        Widget.objects.filter(pk=self.widget.pk).update(status='cancelled')

        self.resume(job)

        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        self.assert_progress(1, 'page:0,', status='cancelled')
        self.assertEqual(TransitionMessage.objects.count(), 1)
