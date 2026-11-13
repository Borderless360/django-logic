"""Temporary failures rebuilding a valid process keep its saved job retryable."""
from django.db import connections, OperationalError
from django.test import TestCase, override_settings

from django_logic import Process, ProcessManager
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from django_logic.state import State
from tests import dl_settings, unbind_model_process
from tests.background.models import Widget


def record_work(instance, **kwargs):
    instance.se_log += 'ran,'
    instance.save(update_fields=['se_log'])


class RestoreRetryState(State):
    query_unavailable_table = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.query_unavailable_table:
            with connections[self.instance._state.db].cursor() as cursor:
                cursor.execute('SELECT id FROM unavailable_restore_dependency')


class RestoreRetryProcess(Process):
    process_name = 'restore_retry'
    state_class = RestoreRetryState
    constructor_error = None
    transitions = [
        BackgroundTransition(
            'run', sources=['draft'], target='done',
            in_progress_state='working', failed_state='failed',
            side_effects=[record_work],
        ),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.constructor_error is not None:
            raise self.constructor_error


@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_MAX_ERRORS=2))
class RestoreRetryTests(TestCase):
    def setUp(self):
        ProcessManager.bind_model_process(Widget, RestoreRetryProcess, state_field='status')
        self.addCleanup(unbind_model_process, Widget, RestoreRetryProcess)
        self.addCleanup(self.clear_failure)

    @staticmethod
    def clear_failure():
        RestoreRetryProcess.constructor_error = None
        RestoreRetryState.query_unavailable_table = False

    def enqueue(self):
        widget = Widget.objects.create()
        widget.restore_retry.run()
        return widget, TransitionMessage.for_instance(widget, 'restore_retry').get()

    def assert_retry_then_success(self, widget, job):
        run_background_transition(job.pk)
        job.refresh_from_db()
        widget.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 1)
        self.assertNotIn('[unrestorable]', job.last_error_message)
        self.assertEqual((widget.status, widget.se_log), ('working', ''))

        self.clear_failure()
        run_background_transition(job.pk)
        run_background_transition(job.pk)
        job.refresh_from_db()
        widget.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertEqual(job.completion_kind, 'succeeded')
        self.assertEqual(job.errors_count, 1)
        self.assertEqual((widget.status, widget.se_log), ('done', 'ran,'))

    def test_process_constructor_errors_keep_the_job_for_retry(self):
        for error in (OperationalError('connection temporarily unavailable'), ValueError('dependency not ready')):
            with self.subTest(error=type(error).__name__):
                widget, job = self.enqueue()
                RestoreRetryProcess.constructor_error = error
                try:
                    self.assert_retry_then_success(widget, job)
                finally:
                    self.clear_failure()

    def test_database_error_in_custom_state_does_not_break_failure_recording(self):
        widget, job = self.enqueue()
        RestoreRetryState.query_unavailable_table = True
        self.assert_retry_then_success(widget, job)

    def test_repeated_constructor_failure_stops_at_the_error_limit(self):
        widget, job = self.enqueue()
        RestoreRetryProcess.constructor_error = OperationalError('connection temporarily unavailable')
        run_background_transition(job.pk)
        run_background_transition(job.pk)
        job.refresh_from_db()
        widget.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertEqual((job.errors_count, job.completion_kind), (2, 'failed'))
        self.assertNotIn('[unrestorable]', job.last_error_message)
        self.assertEqual((widget.status, widget.se_log), ('working', ''))

    def test_unknown_saved_field_is_a_permanent_identity_error(self):
        widget, job = self.enqueue()
        job.field_name = 'removed_state_field'
        job.save(update_fields=['field_name'])
        run_background_transition(job.pk)
        job.refresh_from_db()
        widget.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertEqual(job.errors_count, 0)
        self.assertIn('[unrestorable]', job.last_error_message)
        self.assertEqual((widget.status, widget.se_log), ('working', ''))
