"""Trusted application context follows saved work and never leaks into another job."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
import os
import unittest

from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition, Defer, PermanentFailure
from django_logic.background.pull import run_once
from django_logic.background.context import JobContextError
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import finalize_stuck_attempt, run_background_transition
from django_logic.process import _transition_context
from tests import dl_settings, unbind_model_process
from tests.background.models import Widget
from tests.stability.base import requires_postgres


current_origin = ContextVar('test_job_origin', default=None)
observations = []


class JobContext:
    def capture(self):
        return current_origin.get()

    @contextmanager
    def restore(self, value):
        token = current_origin.set(value)
        try:
            yield
        finally:
            current_origin.reset(token)


class BrokenRestore(JobContext):
    @contextmanager
    def restore(self, value):
        raise ValueError('Saved context cannot be restored')
        yield


class BrokenExit(JobContext):
    @contextmanager
    def restore(self, value):
        with super().restore(value):
            yield
        raise ValueError('Application context cleanup failed')


class AlwaysBrokenExit(JobContext):
    @contextmanager
    def restore(self, value):
        with super().restore(value):
            try:
                yield
            finally:
                raise RuntimeError('Application context cleanup failed')


class SuppressingRestore(JobContext):
    @contextmanager
    def restore(self, value):
        with super().restore(value):
            try:
                yield
            except ValueError:
                pass


def observe(instance, **kwargs):
    observations.append(('work', current_origin.get(), dict(_transition_context.get() or {})))
    if kwargs.get('postpone'):
        raise Defer(until=timezone.now() + timedelta(seconds=10), reason='Waiting for a test result')
    if kwargs.get('permanent'):
        raise PermanentFailure('The test operation was refused')
    if kwargs.get('fail'):
        raise ValueError('The test dependency failed')
    if kwargs.get('submit_successor'):
        Widget.objects.create().context_process.work()


def observe_callback(instance, **kwargs):
    observations.append(('callback', current_origin.get(), dict(_transition_context.get() or {})))
    instance.context_audit.record()


def observe_failure(instance, exception, **kwargs):
    observations.append(('failure', current_origin.get(), dict(_transition_context.get() or {})))
    instance.context_audit.record()


def observe_nested(instance, **kwargs):
    observations.append(('nested', current_origin.get(), dict(_transition_context.get() or {})))


class ContextProcess(Process):
    process_name = 'context_process'
    transitions = [BackgroundTransition(
        'work', sources=['draft'], target='done', in_progress_state='busy', failed_state='failed',
        side_effects=[observe], callbacks=[observe_callback], failure_callbacks=[observe_failure],
    )]


class ContextAudit(Process):
    process_name = 'context_audit'
    transitions = [Transition(
        'record', sources=['done', 'failed'], target=None, side_effects=[observe_nested],
    )]


@override_settings(DJANGO_LOGIC=dl_settings(
    BACKGROUND_EXECUTION='pull', JOB_CONTEXT=f'{__name__}.JobContext',
    TRANSITION_MESSAGE_MAX_ERRORS=2, TRANSITION_MESSAGE_RETRY_MINUTES=0,
))
class JobContextTests(TestCase):
    def setUp(self):
        cache.clear()
        observations.clear()
        self.token = current_origin.set(None)
        self.addCleanup(current_origin.reset, self.token)
        for process in (ContextProcess, ContextAudit):
            ProcessManager.bind_model_process(Widget, process, state_field='status')
            self.addCleanup(unbind_model_process, Widget, process)

    def submit(self, **kwargs):
        widget = Widget.objects.create()
        widget.context_process.work(**kwargs)
        return widget, TransitionMessage.objects.get(instance_id=str(widget.pk), process_name='context_process')

    def test_context_is_separate_from_caller_kwargs_and_covers_callbacks(self):
        origin = {'source': 'connector', 'actor': 42}
        current_origin.set(origin)
        widget, job = self.submit(job_context={'source': 'caller'}, root_id='trace-root')
        origin['source'] = 'changed-after-submit'
        self.assertEqual(job.job_context, {'source': 'connector', 'actor': 42})
        current_origin.set({'source': 'another-request'})
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertEqual([kind for kind, _, _ in observations], ['work', 'callback', 'nested'])
        for _, context, trace in observations:
            self.assertEqual(context, {'source': 'connector', 'actor': 42})
            self.assertEqual(trace['root_id'], 'trace-root')
        self.assertEqual(current_origin.get(), {'source': 'another-request'})
        self.assertIsNone(_transition_context.get())

    def test_retry_and_terminal_failure_restore_the_saved_origin(self):
        current_origin.set({'source': 'connector'})
        _, job = self.submit(fail=True, root_id='failure-root')
        current_origin.set(None)
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 1)
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertEqual([kind for kind, _, _ in observations], ['work', 'work', 'failure', 'nested'])
        for _, context, trace in observations:
            self.assertEqual(context, {'source': 'connector'})
            self.assertEqual(trace['root_id'], 'failure-root')
        self.assertIsNone(current_origin.get())

    def test_successor_captures_restored_context(self):
        current_origin.set({'source': 'connector'})
        _, job = self.submit(submit_successor=True)
        current_origin.set(None)
        run_background_transition(job.pk)
        successor = TransitionMessage.objects.exclude(pk=job.pk).get()
        self.assertEqual(successor.job_context, {'source': 'connector'})
        run_background_transition(successor.pk)
        self.assertTrue(all(context == {'source': 'connector'} for _, context, _ in observations))
        self.assertIsNone(current_origin.get())

    def test_old_job_clears_ambient_context(self):
        _, job = self.submit()
        self.assertIsNone(job.job_context)
        current_origin.set({'source': 'unrelated'})
        run_background_transition(job.pk)
        self.assertTrue(all(context is None for _, context, _ in observations))
        self.assertEqual(current_origin.get(), {'source': 'unrelated'})

    def test_maintenance_failure_callbacks_restore_context(self):
        current_origin.set({'source': 'connector'})
        _, job = self.submit(root_id='maintenance-root')
        TransitionMessage.objects.filter(pk=job.pk).update(errors_count=2, last_error_message='Job process stopped')
        current_origin.set(None)
        self.assertTrue(finalize_stuck_attempt(job.pk))
        self.assertEqual([kind for kind, _, _ in observations], ['failure', 'nested'])
        for _, context, trace in observations:
            self.assertEqual(context, {'source': 'connector'})
            self.assertEqual(trace['root_id'], 'maintenance-root')
        self.assertIsNone(current_origin.get())

    def test_defer_restores_the_previous_context_without_spending_an_error(self):
        current_origin.set({'source': 'connector'})
        _, job = self.submit(postpone=True)
        current_origin.set(None)
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.errors_count, 0)
        self.assertIsNotNone(job.eligible_at)
        self.assertIsNone(current_origin.get())

    def test_unserializable_capture_rolls_back_submission(self):
        current_origin.set(object())
        widget = Widget.objects.create()
        with self.assertRaises(TypeError):
            widget.context_process.work()
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'draft')
        self.assertFalse(TransitionMessage.objects.exists())

    @override_settings(DJANGO_LOGIC=dl_settings(
        BACKGROUND_EXECUTION='pull', JOB_CONTEXT=f'{__name__}.BrokenRestore',
        TRANSITION_MESSAGE_MAX_ERRORS=2, TRANSITION_MESSAGE_RETRY_MINUTES=0,
    ))
    def test_restore_failure_retries_then_completes_without_business_work(self):
        widget, job = self.submit()
        for expected_errors in (1, 2):
            run_background_transition(job.pk)
            job.refresh_from_db()
            self.assertEqual(job.errors_count, expected_errors)
            self.assertEqual(job.is_completed, expected_errors == 2)
        self.assertTrue(job.ended_in_failure)
        self.assertIn('[unrestorable]', job.last_error_message)
        self.assertIn('JOB_CONTEXT', job.last_error_message)
        self.assertEqual(job.start_attempt_count, 2)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'busy')
        self.assertEqual(observations, [])
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.errors_count, 2)

    @override_settings(DJANGO_LOGIC=dl_settings(
        BACKGROUND_EXECUTION='pull', JOB_CONTEXT=f'{__name__}.BrokenRestore',
        TRANSITION_MESSAGE_MAX_ERRORS=2,
    ))
    def test_maintenance_can_complete_a_job_when_its_context_cannot_restore(self):
        widget, job = self.submit()
        TransitionMessage.objects.filter(pk=job.pk).update(
            errors_count=2, eligible_at=timezone.now() + timedelta(minutes=10),
            deferral_reason='Waiting for a dependency',
        )
        self.assertTrue(finalize_stuck_attempt(job.pk))
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertTrue(job.ended_in_failure)
        self.assertEqual(job.errors_count, 2)
        self.assertIsNone(job.eligible_at)
        self.assertEqual(job.deferral_reason, '')
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'busy')
        self.assertEqual(observations, [])

    @override_settings(DJANGO_LOGIC=dl_settings(
        BACKGROUND_EXECUTION='sync', JOB_CONTEXT=f'{__name__}.BrokenRestore',
        TRANSITION_MESSAGE_MAX_ERRORS=2,
    ))
    def test_inline_restore_failure_is_recorded_and_raised(self):
        widget = Widget.objects.create()
        with self.assertRaisesMessage(JobContextError, 'Saved context cannot be restored'):
            widget.context_process.work()
        job = TransitionMessage.objects.get(instance_id=str(widget.pk))
        self.assertEqual(job.errors_count, 1)
        self.assertFalse(job.is_completed)
        self.assertEqual(observations, [])

    @override_settings(DJANGO_LOGIC=dl_settings(
        BACKGROUND_EXECUTION='sync', JOB_CONTEXT=f'{__name__}.SuppressingRestore',
        TRANSITION_MESSAGE_MAX_ERRORS=2,
    ))
    def test_context_adapter_cannot_suppress_a_required_work_failure(self):
        widget = Widget.objects.create()
        with self.assertRaisesMessage(ValueError, 'The test dependency failed'):
            widget.context_process.work(fail=True)
        job = TransitionMessage.objects.get(instance_id=str(widget.pk))
        self.assertEqual(job.errors_count, 1)
        self.assertFalse(job.is_completed)

    @override_settings(DJANGO_LOGIC=dl_settings(
        BACKGROUND_EXECUTION='sync', JOB_CONTEXT=f'{__name__}.AlwaysBrokenExit',
        TRANSITION_MESSAGE_MAX_ERRORS=3,
    ))
    def test_cleanup_failure_preserves_the_original_inline_work_error(self):
        widget = Widget.objects.create()
        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            with self.assertRaisesMessage(ValueError, 'The test dependency failed'):
                widget.context_process.work(fail=True)
        self.assertTrue(any('JOB_CONTEXT cleanup failed' in line for line in logs.output))
        job = TransitionMessage.objects.get(instance_id=str(widget.pk))
        self.assertEqual(job.errors_count, 1)
        self.assertIn('The test dependency failed', job.last_error_message)
        self.assertNotIn('cleanup failed', job.last_error_message)
        self.assertFalse(job.is_completed)
        self.assertIsNone(current_origin.get())

    @override_settings(DJANGO_LOGIC=dl_settings(
        BACKGROUND_EXECUTION='pull', JOB_CONTEXT=f'{__name__}.BrokenExit',
    ))
    def test_adapter_exit_failure_does_not_retry_completed_work(self):
        current_origin.set({'source': 'connector'})
        widget, job = self.submit()
        current_origin.set(None)
        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            run_background_transition(job.pk)
        self.assertTrue(any('JOB_CONTEXT cleanup failed' in line for line in logs.output))
        job.refresh_from_db()
        widget.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        self.assertEqual(job.errors_count, 0)
        self.assertEqual(widget.status, 'done')
        self.assertEqual([kind for kind, _, _ in observations], ['work', 'callback', 'nested'])
        self.assertIsNone(current_origin.get())


class ContextNonAtomicProcess(Process):
    process_name = 'context_non_atomic'
    transitions = [BackgroundTransition(
        'work', sources=['draft'], target='done', in_progress_state='busy', failed_state='failed',
        atomic=False, side_effects=[observe], callbacks=[observe_callback], failure_callbacks=[observe_failure],
    )]


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(
    BACKGROUND_EXECUTION='pull', JOB_CONTEXT=f'{__name__}.JobContext',
    TRANSITION_MESSAGE_MAX_ERRORS=1, TRANSITION_MESSAGE_RETRY_MINUTES=0,
))
class NonAtomicJobContextTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        observations.clear()
        self.token = current_origin.set(None)
        self.addCleanup(current_origin.reset, self.token)
        for process in (ContextNonAtomicProcess, ContextAudit):
            ProcessManager.bind_model_process(Widget, process, state_field='status')
            self.addCleanup(unbind_model_process, Widget, process)

    def test_non_atomic_work_and_success_and_failure_callbacks_restore_original_context(self):
        for fail, expected in [(False, ['work', 'callback', 'nested']), (True, ['work', 'failure', 'nested'])]:
            with self.subTest(fail=fail):
                observations.clear()
                current_origin.set({'source': 'connector'})
                widget = Widget.objects.create()
                widget.context_non_atomic.work(fail=fail)
                job = TransitionMessage.objects.get(instance_id=str(widget.pk), process_name='context_non_atomic')
                current_origin.set({'source': 'other-request'})
                run_background_transition(job.pk)
                job.refresh_from_db()
                self.assertTrue(job.is_completed)
                self.assertEqual(job.ended_in_failure, fail)
                self.assertEqual([kind for kind, _, _ in observations], expected)
                self.assertTrue(all(context == {'source': 'connector'} for _, context, _ in observations))
                self.assertEqual(current_origin.get(), {'source': 'other-request'})


@requires_postgres
@unittest.skipUnless(hasattr(os, 'fork'), 'needs real job processes')
@override_settings(DJANGO_LOGIC=dl_settings(
    BACKGROUND_EXECUTION='pull', JOB_CONTEXT=f'{__name__}.AlwaysBrokenExit',
    TRANSITION_MESSAGE_MAX_ERRORS=3, TRANSITION_MESSAGE_RETRY_MINUTES=0,
))
class WorkerContextCleanupTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        for process in (ContextProcess, ContextNonAtomicProcess, ContextAudit):
            ProcessManager.bind_model_process(Widget, process, state_field='status')
            self.addCleanup(unbind_model_process, Widget, process)

    def test_cleanup_failure_does_not_spend_a_second_error_in_atomic_job_process(self):
        self._check_failure_count('context_process')

    def test_cleanup_failure_does_not_spend_a_second_error_in_non_atomic_job_process(self):
        self._check_failure_count('context_non_atomic')

    def _check_failure_count(self, process_name):
        widget = Widget.objects.create()
        getattr(widget, process_name).work(fail=True)
        job = TransitionMessage.objects.get(instance_id=str(widget.pk), process_name=process_name)
        for attempt in range(1, 4):
            self.assertTrue(run_once([job.queue_name], isolate=True))
            job.refresh_from_db()
            self.assertEqual(job.errors_count, attempt)
            self.assertEqual(job.start_attempt_count, attempt)
            self.assertEqual(job.is_completed, attempt == 3)
            self.assertIn('The test dependency failed', job.last_error_message)
            self.assertNotIn('cleanup failed', job.last_error_message)
        self.assertTrue(job.ended_in_failure)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'failed')
