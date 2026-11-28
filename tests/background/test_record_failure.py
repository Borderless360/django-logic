"""Required business failure changes and a failed job are committed together."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import tempfile
from threading import Event
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError, connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition, Defer, PermanentFailure
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import run_once
from django_logic.background.runner import finalize_stuck_attempt, run_background_transition
from django_logic.background.safety_nets import detect_stuck_transitions
from tests import dl_settings, unbind_model_process
from tests.background.models import Widget
from tests.stability.base import requires_postgres


QUEUE = 'required-business-failure'
SETTINGS = dl_settings(
    BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_MAX_ERRORS=2,
    TRANSITION_MESSAGE_RETRY_MINUTES=0,
)
WORK_CALLS = []
RECORD_CALLS = []
RECORD_ERROR = []
CONTEXT_ERROR = []
LEGACY_CALLS = []


class ProviderRefused(PermanentFailure):
    pass


class FailureJobContext:
    def capture(self):
        return {'source': 'required-failure-test'}

    @contextmanager
    def restore(self, value):
        if CONTEXT_ERROR:
            raise OperationalError('temporary context database failure')
        yield


def do_work(instance, work_mode='permanent', request_path='', **kwargs):
    WORK_CALLS.append(instance.pk)
    if request_path and not instance.se_log:
        with Path(request_path).open('a') as requests:
            requests.write('request\n')
        instance.se_log = 'receipt'
        instance.save(update_fields=['se_log'])
    if work_mode == 'crash':
        os._exit(19)
    if work_mode == 'defer':
        raise Defer(until=timezone.now() + timedelta(minutes=1), reason='provider waiting')
    if work_mode == 'retry':
        raise ValueError('provider unavailable')
    if work_mode == 'success':
        return
    if work_mode == 'typed':
        raise ProviderRefused('provider refused this particular item')
    raise PermanentFailure('provider rejected this work')


def may_record_failure(instance, user):
    return user.is_staff


def save_failure_reason(instance, *, reason, original_action, matched_types, user=None, **kwargs):
    instance.kwargs_seen = [*instance.kwargs_seen, {
        'reason': reason,
        'original_action': original_action,
        'actor': user.pk if user else None,
        'matched_types': matched_types,
    }]
    instance.save(update_fields=['kwargs_seen'])


def record_business_failure(instance, *, exception, tr_id, user=None, stop_after_record='', **kwargs):
    from django_logic.background import failure_matches

    using = instance._state.db
    RECORD_CALLS.append((instance.pk, using, connections[using].in_atomic_block, user.pk if user else None))
    # Completion is visible in this transaction, so the ordinary action can run.
    job = TransitionMessage.objects.using(using).get(instance_id=str(instance.pk), transition_name='work')
    if not job.is_completed or not job.ended_in_failure:
        raise AssertionError('The required failure action must share the completed job transaction.')
    FailureProcess(instance=instance, field_name='status').fail(
        user=user, reason=str(exception), original_action=tr_id,
        matched_types={
            'specific': failure_matches(exception, ProviderRefused),
            'permanent': failure_matches(exception, PermanentFailure),
            'either': failure_matches(exception, (ValueError, ProviderRefused)),
            'unrelated': failure_matches(exception, ValueError),
        },
    )
    if RECORD_ERROR:
        if RECORD_ERROR[0] == 'database':
            with connections[using].cursor() as cursor:
                cursor.execute('SELECT * FROM missing_required_failure_test_table')
        if RECORD_ERROR[0] == 'swallow_database':
            try:
                Widget.objects.using(using).create(pk=instance.pk)
            except Exception:
                pass
        else:
            raise RuntimeError('recording temporarily failed')
    if stop_after_record:
        marker = Path(stop_after_record)
        if not marker.exists():
            marker.write_text('business failure written before job process stopped')
            os._exit(23)


def after_failure(instance, **kwargs):
    instance.cb_log += 'failure,'
    instance.save(update_fields=['cb_log'])


class FailureProcess(Process):
    process_name = 'required_failure'
    transitions = [
        Transition('fail', ['working'], target='action_required', permissions=[may_record_failure],
                   side_effects=[save_failure_reason]),
        BackgroundTransition('work', ['draft'], target='done', in_progress_state='working',
                             side_effects=[do_work], record_failure=record_business_failure,
                             failure_callbacks=[after_failure], queue=QUEUE),
    ]


class NonAtomicFailureProcess(FailureProcess):
    transitions = [
        BackgroundTransition('work', ['draft'], target='done', in_progress_state='working',
                             side_effects=[do_work], record_failure=record_business_failure,
                             failure_callbacks=[after_failure], queue=QUEUE, atomic=False),
    ]


def legacy_application_failure(*args, **kwargs):
    LEGACY_CALLS.append('application-owned attribute')


class LegacyAttributeBackgroundTransition(BackgroundTransition):
    def __init__(self, *args, **kwargs):
        self.record_failure = legacy_application_failure
        super().__init__(*args, **kwargs)


class LegacyFailureProcess(Process):
    process_name = 'legacy_failure'
    transitions = [LegacyAttributeBackgroundTransition(
        'work', ['draft'], target='done', in_progress_state='working', failed_state='legacy_failed',
        side_effects=[do_work], queue=QUEUE,
    )]


class LegacyExplicitFailureProcess(Process):
    process_name = 'legacy_explicit_failure'
    transitions = [LegacyAttributeBackgroundTransition(
        'work', ['draft'], target='done', in_progress_state='working', side_effects=[do_work],
        record_failure=record_business_failure, failure_callbacks=[after_failure], queue=QUEUE,
    )]


class RecordFailureDeclarationTests(SimpleTestCase):
    def test_required_failure_handler_must_be_callable(self):
        for value in ('handler', [], 1):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                BackgroundTransition('work', ['draft'], record_failure=value)

    def test_required_failure_handler_and_failed_state_are_alternatives(self):
        with self.assertRaises(ImproperlyConfigured):
            BackgroundTransition('work', ['draft'], record_failure=record_business_failure, failed_state='failed')

    def test_constructor_preserves_the_application_owned_failure_attribute(self):
        plain = LegacyAttributeBackgroundTransition('work', ['draft'])
        explicit = LegacyAttributeBackgroundTransition('work', ['draft'], record_failure=record_business_failure)
        self.assertIs(plain.record_failure, legacy_application_failure)
        self.assertIsNone(plain._required_failure_recorder)
        self.assertIs(explicit.record_failure, legacy_application_failure)
        self.assertIs(explicit._required_failure_recorder, record_business_failure)

    def test_hook_validation_ignores_the_legacy_attribute(self):
        for process in (LegacyFailureProcess, LegacyExplicitFailureProcess):
            with self.subTest(process=process):
                ProcessManager.bind_model_process(Widget, process, state_field='status')
                self.addCleanup(unbind_model_process, Widget, process)


class FailureFixture:
    def setUp(self):
        super().setUp()
        cache.clear()
        for observations in (WORK_CALLS, RECORD_CALLS, RECORD_ERROR, CONTEXT_ERROR, LEGACY_CALLS):
            observations.clear()
            self.addCleanup(observations.clear)
        self.widget = Widget.objects.create()
        self.user = get_user_model().objects.create(username='permitted-actor', is_staff=True)

    def enqueue(self, *, process=FailureProcess, user='default', **kwargs):
        if user == 'default':
            user = self.user
        process(instance=self.widget, field_name='status').work(user=user, **kwargs)
        return TransitionMessage.objects.get(instance_id=str(self.widget.pk), transition_name='work')

    def make_exhausted(self, job):
        TransitionMessage.objects.filter(pk=job.pk).update(
            errors_count=2, last_error_message='[crashed] the job process stopped',
            last_error_dt=timezone.now() - timedelta(minutes=1),
        )

    def assert_recorded_once(self, job, *, actor='default'):
        if actor == 'default':
            actor = self.user.pk
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertTrue(job.ended_in_failure)
        self.assertEqual(job.completion_kind, 'failed')
        self.assertEqual(self.widget.status, 'action_required')
        self.assertEqual(len(self.widget.kwargs_seen), 1)
        self.assertEqual(self.widget.kwargs_seen[0]['original_action'], job.kwargs['tr_id'])
        self.assertEqual(self.widget.kwargs_seen[0]['actor'], actor)
        self.assertEqual(self.widget.cb_log, 'failure,')

    def assert_required_changes_pending(self, job):
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertTrue(job.failure_pending)
        self.assertEqual(self.widget.status, 'working')
        self.assertEqual(self.widget.kwargs_seen, [])
        self.assertEqual(self.widget.cb_log, '')


@override_settings(DJANGO_LOGIC=SETTINGS)
class RecordFailureTests(FailureFixture, TestCase):
    def test_legacy_failure_attribute_is_not_automatically_invoked(self):
        job = self.enqueue(process=LegacyFailureProcess)
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertTrue(job.ended_in_failure)
        self.assertEqual(self.widget.status, 'legacy_failed')
        self.assertEqual((LEGACY_CALLS, RECORD_CALLS), ([], []))

    def test_explicit_handler_is_independent_of_legacy_application_attribute(self):
        job = self.enqueue(process=LegacyExplicitFailureProcess)
        run_background_transition(job.pk)
        self.assert_recorded_once(job)
        self.assertEqual(LEGACY_CALLS, [])
        self.assertEqual(len(RECORD_CALLS), 1)

    def test_permanent_failure_records_reason_and_job_once(self):
        job = self.enqueue()
        run_background_transition(job.pk)
        self.assert_recorded_once(job)
        self.assertEqual(self.widget.kwargs_seen[0]['reason'], 'provider rejected this work')
        self.assertEqual(RECORD_CALLS, [(self.widget.pk, 'default', True, self.user.pk)])
        run_background_transition(job.pk)
        self.assertFalse(finalize_stuck_attempt(job.pk))
        self.assert_recorded_once(job)
        self.assertEqual(WORK_CALLS, [self.widget.pk])

    def test_retryable_work_records_failure_only_after_error_budget(self):
        job = self.enqueue(work_mode='retry')
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 1)
        self.assertEqual(RECORD_CALLS, [])
        run_background_transition(job.pk)
        self.assert_recorded_once(job)
        self.assertEqual(WORK_CALLS, [self.widget.pk, self.widget.pk])

    def test_normal_failure_matches_its_specific_type_and_base_class(self):
        job = self.enqueue(work_mode='typed')
        run_background_transition(job.pk)
        self.assert_recorded_once(job)
        self.assertEqual(self.widget.kwargs_seen[0]['matched_types'], {
            'specific': True, 'permanent': True, 'either': True, 'unrelated': False,
        })

    def test_maintenance_uses_same_failure_action_and_saved_actor(self):
        job = self.enqueue()
        self.make_exhausted(job)
        self.assertEqual(detect_stuck_transitions(), 1)
        self.assert_recorded_once(job)
        self.assertIn('[crashed]', self.widget.kwargs_seen[0]['reason'])
        self.assertEqual(self.widget.kwargs_seen[0]['matched_types'], {
            'specific': False, 'permanent': False, 'either': False, 'unrelated': False,
        })
        self.assertEqual(detect_stuck_transitions(), 0)
        self.assertEqual(WORK_CALLS, [])
        self.assertEqual(len(RECORD_CALLS), 1)

    def test_system_job_keeps_its_absent_actor(self):
        job = self.enqueue(user=None)
        run_background_transition(job.pk)
        self.assert_recorded_once(job, actor=None)

    def test_deferral_does_not_record_business_failure(self):
        job = self.enqueue(work_mode='defer')
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 0)
        self.assertEqual(job.deferral_count, 1)
        self.assertFalse(job.failure_pending)
        self.assertEqual(self.widget.status, 'working')
        self.assertEqual(RECORD_CALLS, [])
        self.assertEqual(detect_stuck_transitions(), 0)

    def test_success_does_not_record_business_failure(self):
        job = self.enqueue(work_mode='success')
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        self.assertEqual(self.widget.status, 'done')
        self.assertEqual(RECORD_CALLS, [])

    def test_handler_failure_rolls_back_business_changes_and_retries_only_recording(self):
        job = self.enqueue()
        RECORD_ERROR.append('ordinary')
        run_background_transition(job.pk)
        self.assert_required_changes_pending(job)
        self.assertEqual(WORK_CALLS, [self.widget.pk])
        run_background_transition(job.pk)
        self.assertEqual(WORK_CALLS, [self.widget.pk])
        RECORD_ERROR.clear()
        self.assertEqual(detect_stuck_transitions(), 1)
        self.assert_recorded_once(job)
        self.assertEqual(WORK_CALLS, [self.widget.pk])
        self.assertEqual(detect_stuck_transitions(), 0)

    def test_pending_failure_keeps_waiting_if_handler_is_removed_from_declaration(self):
        job = self.enqueue()
        RECORD_ERROR.append('ordinary')
        run_background_transition(job.pk)
        self.assert_required_changes_pending(job)
        RECORD_ERROR.clear()
        RECORD_CALLS.clear()
        with patch.object(FailureProcess.transitions[1], '_required_failure_recorder', None):
            self.assertEqual(detect_stuck_transitions(), 0)
        self.assert_required_changes_pending(job)
        self.assertIn('record_failure', job.failure_side_effect_error)
        self.assertEqual(RECORD_CALLS, [])
        self.assertEqual(detect_stuck_transitions(), 1)
        self.assert_recorded_once(job)
        self.assertEqual(len(RECORD_CALLS), 1)
        self.assertEqual(WORK_CALLS, [self.widget.pk])

    def test_database_error_rolls_back_required_failure_changes(self):
        job = self.enqueue()
        RECORD_ERROR.append('database')
        run_background_transition(job.pk)
        self.assert_required_changes_pending(job)
        RECORD_ERROR.clear()
        self.assertTrue(finalize_stuck_attempt(job.pk))
        self.assert_recorded_once(job)
        self.assertEqual(WORK_CALLS, [self.widget.pk])

    def test_suppressed_database_error_does_not_complete_job(self):
        job = self.enqueue()
        RECORD_ERROR.append('swallow_database')
        run_background_transition(job.pk)
        self.assert_required_changes_pending(job)
        RECORD_ERROR.clear()
        self.assertTrue(finalize_stuck_attempt(job.pk))
        self.assert_recorded_once(job)

    def test_failure_action_still_checks_original_user_permissions(self):
        job = self.enqueue()
        get_user_model().objects.filter(pk=self.user.pk).update(is_staff=False)
        run_background_transition(job.pk)
        self.assert_required_changes_pending(job)
        get_user_model().objects.filter(pk=self.user.pk).update(is_staff=True)
        self.assertTrue(finalize_stuck_attempt(job.pk))
        self.assert_recorded_once(job)
        self.assertEqual(WORK_CALLS, [self.widget.pk])

    def test_deleted_actor_does_not_run_work_or_required_failure(self):
        job = self.enqueue()
        self.user.delete()
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertTrue(job.ended_in_failure)
        self.assertIn('[unrestorable]', job.last_error_message)
        self.assertEqual(self.widget.status, 'working')
        self.assertEqual(self.widget.kwargs_seen, [])
        self.assertEqual((WORK_CALLS, RECORD_CALLS), ([], []))

    def test_deleted_actor_during_maintenance_does_not_run_required_failure(self):
        job = self.enqueue()
        self.make_exhausted(job)
        self.user.delete()
        self.assertTrue(finalize_stuck_attempt(job.pk))
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertEqual(self.widget.status, 'working')
        self.assertEqual(self.widget.kwargs_seen, [])
        self.assertEqual(RECORD_CALLS, [])

    def test_malformed_actor_during_maintenance_cannot_become_system_work(self):
        job = self.enqueue()
        self.make_exhausted(job)
        TransitionMessage.objects.filter(pk=job.pk).update(kwargs={**job.kwargs, 'user_id': ['invalid']})
        finalize_stuck_attempt(job.pk)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'working')
        self.assertEqual(self.widget.kwargs_seen, [])
        self.assertEqual(RECORD_CALLS, [])

    def test_transient_actor_restore_error_leaves_failure_recording_pending(self):
        job = self.enqueue()
        self.make_exhausted(job)
        with patch('django_logic.background.runner.deserialize_kwargs',
                   side_effect=OperationalError('temporary database failure')):
            self.assertEqual(detect_stuck_transitions(), 0)
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(self.widget.status, 'working')
        self.assertEqual(RECORD_CALLS, [])
        self.assertTrue(finalize_stuck_attempt(job.pk))
        self.assert_recorded_once(job)

    @override_settings(DJANGO_LOGIC={**SETTINGS, 'JOB_CONTEXT': f'{__name__}.FailureJobContext'})
    def test_transient_context_restore_error_keeps_required_failure_pending(self):
        job = self.enqueue()
        self.make_exhausted(job)
        TransitionMessage.objects.filter(pk=job.pk).update(failure_pending=True)
        CONTEXT_ERROR.append(True)
        self.assertEqual(detect_stuck_transitions(), 0)
        self.assert_required_changes_pending(job)
        self.assertEqual((WORK_CALLS, RECORD_CALLS), ([], []))
        CONTEXT_ERROR.clear()
        self.assertEqual(detect_stuck_transitions(), 1)
        self.assert_recorded_once(job)
        self.assertEqual(WORK_CALLS, [])
        self.assertEqual(len(RECORD_CALLS), 1)
        self.assertEqual(detect_stuck_transitions(), 0)

    def test_newer_business_state_is_not_replaced_by_failure_action(self):
        job = self.enqueue()
        self.make_exhausted(job)
        Widget.objects.filter(pk=self.widget.pk).update(status='cancelled')
        self.assertTrue(finalize_stuck_attempt(job.pk))
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertEqual(job.completion_kind, 'superseded')
        self.assertEqual(self.widget.status, 'cancelled')
        self.assertEqual(RECORD_CALLS, [])


@requires_postgres
@override_settings(DJANGO_LOGIC=SETTINGS)
class RecordFailurePostgreSQLTests(FailureFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix='dl_required_failure_')
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_business_failure_and_job_completion_become_visible_together(self):
        job = self.enqueue()
        entered, release = Event(), Event()
        observed = []

        def record_and_observe(instance, **kwargs):
            record_business_failure(instance, **kwargs)
            observer = connections['default'].copy(alias='default')
            try:
                with observer.cursor() as cursor:
                    cursor.execute('SELECT status, jsonb_array_length(kwargs_seen) FROM bg_tests_widget WHERE id = %s', [instance.pk])
                    observed.append(cursor.fetchone())
                    cursor.execute(
                        'SELECT is_completed FROM django_logic_background_transitionmessage WHERE id = %s',
                        [job.pk],
                    )
                    observed.append(cursor.fetchone())
            finally:
                observer.close()
            entered.set()
            if not release.wait(5):
                raise RuntimeError('The test did not release failure recording.')

        def run_job():
            try:
                run_background_transition(job.pk)
            finally:
                connections.close_all()

        with patch.object(FailureProcess.transitions[1], '_required_failure_recorder', record_and_observe):
            with ThreadPoolExecutor(max_workers=1) as executor:
                first = executor.submit(run_job)
                try:
                    self.assertTrue(entered.wait(5), 'The required handler did not reach the test barrier.')
                    self.assertFalse(finalize_stuck_attempt(job.pk))
                    self.widget.refresh_from_db()
                    self.assertEqual(self.widget.status, 'working')
                    self.assertEqual(self.widget.kwargs_seen, [])
                finally:
                    release.set()
                first.result(timeout=5)
        self.assertEqual(observed, [('working', 0), (False,)])
        self.assert_recorded_once(job)
        self.assertEqual(detect_stuck_transitions(), 0)
        self.assertEqual(len(RECORD_CALLS), 1)

    def test_non_atomic_receipt_survives_failure_recording_retry(self):
        requests = self.directory / 'requests'
        job = self.enqueue(process=NonAtomicFailureProcess, request_path=str(requests))
        RECORD_ERROR.append('database')
        run_background_transition(job.pk)
        self.assert_required_changes_pending(job)
        self.assertEqual(self.widget.se_log, 'receipt')
        self.assertEqual(requests.read_text().splitlines(), ['request'])
        RECORD_ERROR.clear()
        self.assertTrue(finalize_stuck_attempt(job.pk))
        self.assert_recorded_once(job)
        self.assertEqual(self.widget.se_log, 'receipt')
        self.assertEqual(requests.read_text().splitlines(), ['request'])
        self.assertTrue(all(alias == 'default' and atomic for _, alias, atomic, _ in RECORD_CALLS))

    @override_settings(DJANGO_LOGIC={**SETTINGS, 'TRANSITION_MESSAGE_MAX_ERRORS': 1})
    def test_real_job_process_loss_on_final_attempt_records_business_failure(self):
        job = self.enqueue(work_mode='crash')
        self.assertTrue(run_once([QUEUE], isolate=True))
        job.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 1)
        self.assertEqual(detect_stuck_transitions(), 1)
        self.assert_recorded_once(job)
        self.assertIn('[crashed]', self.widget.kwargs_seen[0]['reason'])
        self.assertEqual(detect_stuck_transitions(), 0)

    @override_settings(DJANGO_LOGIC={**SETTINGS, 'TRANSITION_MESSAGE_MAX_ERRORS': 1})
    def test_process_loss_after_failure_write_does_not_duplicate_failure_or_provider_request(self):
        requests = self.directory / 'requests'
        stop_marker = self.directory / 'stopped-after-record'
        job = self.enqueue(process=NonAtomicFailureProcess, request_path=str(requests), work_mode='typed',
                           stop_after_record=str(stop_marker))
        self.assertTrue(run_once([QUEUE], isolate=True))
        self.assertTrue(stop_marker.exists())
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertTrue(job.failure_pending)
        self.assertEqual(job.errors_count, 1)
        self.assertEqual(job.last_error_message, 'provider refused this particular item')
        self.assertIn('exit code 23', job.failure_side_effect_error)
        self.assertEqual(self.widget.status, 'working')
        self.assertEqual(self.widget.kwargs_seen, [])
        self.assertEqual(self.widget.se_log, 'receipt')
        self.assertEqual(detect_stuck_transitions(), 1)
        self.assert_recorded_once(job)
        self.assertEqual(self.widget.kwargs_seen[0]['reason'], 'provider refused this particular item')
        self.assertEqual(self.widget.kwargs_seen[0]['matched_types'], {
            'specific': True, 'permanent': True, 'either': True, 'unrelated': False,
        })
        self.assertEqual(detect_stuck_transitions(), 0)
        self.assertEqual(requests.read_text().splitlines(), ['request'])

