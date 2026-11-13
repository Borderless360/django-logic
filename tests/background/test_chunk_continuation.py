"""The long-job recipe saves each required continuation with its result."""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings

from django_logic import DatabaseTransition, Process, ProcessManager
from django_logic.background import BackgroundEffect, BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from tests import dl_settings, unbind_model_process
from tests.background.models import Widget
from tests.stability.base import requires_postgres


def import_next_page(instance, **kwargs):
    cursor = instance.kwargs_seen['cursor']
    if cursor is None:
        return
    instance.se_log += f'page:{cursor},'
    instance.kwargs_seen = {'cursor': 1 if cursor == 0 else None}
    instance.save(update_fields=['se_log', 'kwargs_seen'])


def continue_import(instance, **kwargs):
    if instance.status != 'importing':
        return
    if instance.kwargs_seen['cursor'] is None:
        instance.chunk_import.finish_import()
    else:
        instance.chunk_import.import_page()


def no_more_pages(instance, **kwargs):
    return instance.kwargs_seen['cursor'] is None


class ChunkImportProcess(Process):
    process_name = 'chunk_import'
    transitions = [
        BackgroundTransition(
            action_name='import_page',
            sources=['importing'],
            side_effects=[import_next_page],
            background_effects=[BackgroundEffect('continue_import', continue_import)],
        ),
        DatabaseTransition(
            action_name='finish_import',
            sources=['importing'],
            target='imported',
            conditions=[no_more_pages],
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

    def test_page_and_continuation_decisions_commit_together(self):
        self.widget.chunk_import.import_page()
        first_page = TransitionMessage.objects.get(effect_name='')
        save = TransitionMessage.save

        def fail_after_saving_effect(message, *args, **kwargs):
            result = save(message, *args, **kwargs)
            if message.effect_name == 'continue_import':
                raise RuntimeError('effect save failed')
            return result

        with patch.object(TransitionMessage, 'save', fail_after_saving_effect):
            run_background_transition(first_page.pk)
        first_page.refresh_from_db()
        self.assertFalse(first_page.is_completed)
        self.assertEqual(first_page.errors_count, 1)
        self.assert_progress(0, '')
        self.assertFalse(TransitionMessage.objects.filter(parent_job_id=first_page.pk).exists())

        run_background_transition(first_page.pk)
        first_page.refresh_from_db()
        self.assertTrue(first_page.is_completed)
        self.assertFalse(first_page.ended_in_failure)
        self.assert_progress(1, 'page:0,')
        continuation = TransitionMessage.objects.get(parent_job_id=first_page.pk)
        self.assertFalse(continuation.is_completed)
        self.assertEqual(continuation.effect_name, 'continue_import')
        self.assertEqual(continuation.process_name, 'chunk_import')
        self.assertEqual(continuation.owning_process_class, first_page.owning_process_class)

        complete = TransitionMessage.mark_as_completed

        def fail_after_completing_effect(message, *args, **kwargs):
            result = complete(message, *args, **kwargs)
            if message.effect_name == 'continue_import':
                raise RuntimeError('effect completion failed')
            return result

        with patch.object(TransitionMessage, 'mark_as_completed', fail_after_completing_effect):
            run_background_transition(continuation.pk)
        continuation.refresh_from_db()
        self.assertFalse(continuation.is_completed)
        self.assertEqual(continuation.errors_count, 1)
        self.assertEqual(TransitionMessage.objects.filter(effect_name='').count(), 1)
        self.assert_progress(1, 'page:0,')

        run_background_transition(continuation.pk)
        continuation.refresh_from_db()
        self.assertTrue(continuation.is_completed)
        self.assertFalse(continuation.ended_in_failure)
        second_page = TransitionMessage.objects.get(effect_name='', is_completed=False)
        self.assertEqual(second_page.process_name, 'chunk_import')
        self.assertEqual(second_page.transition_name, 'import_page')
        self.assertEqual(second_page.kwargs['parent_id'], continuation.kwargs['tr_id'])
        self.assertEqual(second_page.kwargs['root_id'], first_page.kwargs['root_id'])

        run_background_transition(second_page.pk)
        self.assert_progress(None, 'page:0,page:1,')
        final_continuation = TransitionMessage.objects.get(parent_job_id=second_page.pk)
        with patch.object(TransitionMessage, 'mark_as_completed', fail_after_completing_effect):
            run_background_transition(final_continuation.pk)
        final_continuation.refresh_from_db()
        self.assertFalse(final_continuation.is_completed)
        self.assertEqual(final_continuation.errors_count, 1)
        self.assert_progress(None, 'page:0,page:1,')

        run_background_transition(final_continuation.pk)
        self.assert_progress(None, 'page:0,page:1,', status='imported')
        self.assertEqual(TransitionMessage.objects.count(), 4)
        self.assertFalse(TransitionMessage.objects.filter(is_completed=False).exists())
        self.assertFalse(TransitionMessage.objects.filter(ended_in_failure=True).exists())
        run_background_transition(continuation.pk)
        run_background_transition(final_continuation.pk)
        self.assertEqual(TransitionMessage.objects.count(), 4)
        self.assert_progress(None, 'page:0,page:1,', status='imported')

    def test_delayed_continuation_leaves_cancelled_import_unchanged(self):
        self.widget.chunk_import.import_page()
        first_page = TransitionMessage.objects.get(effect_name='')
        run_background_transition(first_page.pk)
        continuation = TransitionMessage.objects.get(parent_job_id=first_page.pk)
        Widget.objects.filter(pk=self.widget.pk).update(status='cancelled')

        run_background_transition(continuation.pk)

        continuation.refresh_from_db()
        self.assertTrue(continuation.is_completed)
        self.assertFalse(continuation.ended_in_failure)
        self.assert_progress(1, 'page:0,', status='cancelled')
        self.assertEqual(TransitionMessage.objects.count(), 2)
