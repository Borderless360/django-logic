"""Provider confirmations survive failures without repeating an external request."""
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import InterfaceError, OperationalError, connection, connections, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from django_logic import Process
from django_logic.background import BackgroundTransition, PermanentFailure
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import claim_next, run_once
from django_logic.background.runner import run_background_transition
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres


QUEUE = 'non_atomic_test'
PULL_SETTINGS = dl_settings(
    BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_RETRY_MINUTES=0,
    TRANSITION_MESSAGE_MAX_ERRORS=3,
)


def submit_provider(instance, request_path='', stop='', **kwargs):
    if instance.se_log == 'confirmed':
        return
    if instance.se_log == 'pending':
        raise PermanentFailure('Check the provider before submitting this request again.')
    instance.se_log = 'pending'
    instance.save(update_fields=['se_log'])
    with open(request_path, 'a') as requests:
        requests.write('request\n')
    if stop == 'crash':
        os._exit(3)
    if stop == 'timeout':
        time.sleep(30)
    instance.se_log = 'confirmed'
    instance.save(update_fields=['se_log'])


def prepare_report(instance, **kwargs):
    pass


def fail_report(instance, **kwargs):
    raise ValueError('report failed')


def record_callback(instance, **kwargs):
    assert instance._state.db == 'default'
    assert connection.get_autocommit()
    instance.cb_log += 'callback,'
    instance.save(update_fields=['cb_log'])


class ReceiptProcess(Process):
    process_name = 'receipt_process'
    transitions = [
        BackgroundTransition(
            action_name='submit', sources=['draft'], target='submitted',
            in_progress_state='submitting', failed_state='failed',
            conditions=[], side_effects=[submit_provider, prepare_report],
            callbacks=[record_callback], failure_callbacks=[record_callback],
            queue=QUEUE, timeout=2, atomic=False,
        ),
    ]


class AtomicReceiptProcess(Process):
    process_name = 'atomic_receipt_process'
    transitions = [
        BackgroundTransition(
            action_name='submit', sources=['draft'], target='submitted',
            in_progress_state='submitting', failed_state='failed',
            conditions=[], side_effects=[submit_provider, prepare_report],
            callbacks=[], queue=QUEUE, timeout=2,
        ),
    ]


class AtomicOptionValidationTests(SimpleTestCase):
    def test_atomic_defaults_to_true(self):
        self.assertTrue(BackgroundTransition('run', sources=['ready']).atomic)

    def test_sqlite_is_refused_before_database_work(self):
        from django_logic.background.non_atomic import validate_non_atomic

        with patch.object(connections['default'], 'vendor', 'sqlite'):
            with self.assertRaisesMessage(ImproperlyConfigured, 'default PostgreSQL'):
                validate_non_atomic(Widget(), require_autocommit=False)

    def test_atomic_accepts_only_booleans(self):
        for value in (None, 0, 1, 'false'):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                BackgroundTransition('run', sources=['ready'], atomic=value)


@requires_postgres
@override_settings(DJANGO_LOGIC=PULL_SETTINGS)
class NonAtomicProviderTests(TransactionTestCase):
    databases = '__all__'

    def setUp(self):
        cache.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.request_path = str(Path(self.directory.name) / 'requests')
        self.widget = Widget.objects.create()

    def enqueue(self, process_class=ReceiptProcess, **kwargs):
        process_class(field_name='status', instance=self.widget).submit(
            request_path=self.request_path, **kwargs,
        )
        return TransitionMessage.objects.get(instance_id=str(self.widget.pk))

    def request_count(self):
        path = Path(self.request_path)
        return len(path.read_text().splitlines()) if path.exists() else 0

    def test_confirmed_response_survives_report_failure_and_retry_does_not_resubmit(self):
        transition_message = self.enqueue()
        with patch.object(ReceiptProcess.transitions[0].side_effects, '_commands', [
            submit_provider, fail_report,
        ]):
            run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertEqual(self.widget.status, 'submitting')
        self.assertEqual(transition_message.errors_count, 1)
        self.assertFalse(transition_message.is_completed)
        self.assertEqual(self.request_count(), 1)
        self.assertTrue(run_once([QUEUE], isolate=True))
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.status, 'submitted')
        self.assertEqual(self.widget.cb_log, 'callback,')
        self.assertTrue(transition_message.is_completed)
        self.assertFalse(transition_message.ended_in_failure)
        self.assertEqual(self.request_count(), 1)

    def test_default_atomic_mode_still_rolls_back_provider_receipt(self):
        transition_message = self.enqueue(AtomicReceiptProcess)
        with patch.object(AtomicReceiptProcess.transitions[0].side_effects, '_commands', [
            submit_provider, fail_report,
        ]):
            run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, '')
        self.assertEqual(self.widget.status, 'submitting')
        self.assertEqual(self.request_count(), 1)

    def test_rejected_target_write_retains_confirmation_and_records_failure(self):
        transition_message = self.enqueue()
        original_save = Widget.save

        def reject_target(instance, *args, **kwargs):
            if instance.status == 'submitted':
                raise ValueError('target rejected')
            return original_save(instance, *args, **kwargs)

        with patch.object(Widget, 'save', reject_target):
            run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertEqual(self.widget.status, 'submitting')
        self.assertEqual(transition_message.errors_count, 1)
        self.assertFalse(transition_message.is_completed)
        self.assertIn('target rejected', transition_message.last_error_message)
        run_background_transition(transition_message.pk)
        self.assertEqual(self.request_count(), 1)

    def test_completion_failure_rolls_back_target_but_keeps_provider_confirmation(self):
        transition_message = self.enqueue()
        with patch.object(TransitionMessage, 'mark_as_completed', side_effect=ValueError('completion failed')):
            run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertEqual(self.widget.status, 'submitting')
        self.assertFalse(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 1)
        self.assertIn('completion failed', transition_message.last_error_message)

    def test_terminal_failure_restores_alias_before_failure_callbacks(self):
        transition_message = self.enqueue()

        def refuse(instance, **kwargs):
            raise PermanentFailure('report cannot be generated')

        with patch.object(ReceiptProcess.transitions[0].side_effects, '_commands', [submit_provider, refuse]):
            run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertEqual(self.widget.status, 'failed')
        self.assertEqual(self.widget.cb_log, 'callback,')
        self.assertTrue(transition_message.is_completed)
        self.assertTrue(transition_message.ended_in_failure)

    def test_two_workers_cannot_run_one_provider_request_concurrently(self):
        transition_message = self.enqueue()
        entered = threading.Event()
        release = threading.Event()
        failures = []

        def wait_after_submission(instance, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test did not release the job')

        def first_worker():
            try:
                run_background_transition(transition_message.pk)
            except BaseException as error:
                failures.append(error)
            finally:
                connections.close_all()

        with patch.object(ReceiptProcess.transitions[0].side_effects, '_commands', [submit_provider, wait_after_submission]):
            worker = threading.Thread(target=first_worker)
            worker.start()
            try:
                self.assertTrue(entered.wait(10))
                self.assertIsNone(claim_next([QUEUE]))
                run_background_transition(transition_message.pk)
                self.assertEqual(self.request_count(), 1)
            finally:
                release.set()
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'submitted')
        self.assertEqual(self.request_count(), 1)

    def test_crash_preserves_pending_receipt_and_retry_requires_review(self):
        self.assert_stopped_request_is_not_repeated('crash', '[crashed]')

    def test_timeout_preserves_pending_receipt_and_retry_requires_review(self):
        self.assert_stopped_request_is_not_repeated('timeout', '[timeout]')

    def assert_stopped_request_is_not_repeated(self, stop, marker):
        transition_message = self.enqueue(stop=stop)
        self.assertTrue(run_once([QUEUE], isolate=True))
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'pending')
        self.assertEqual(transition_message.errors_count, 1)
        self.assertIn(marker, transition_message.last_error_message)
        self.assertFalse(transition_message.is_completed)
        self.assertTrue(run_once([QUEUE], isolate=True))
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.request_count(), 1)
        self.assertEqual(self.widget.se_log, 'pending')
        self.assertEqual(self.widget.status, 'failed')
        self.assertEqual(transition_message.errors_count, 2)
        self.assertTrue(transition_message.is_completed)
        self.assertTrue(transition_message.ended_in_failure)
        self.assertIn('Check the provider', transition_message.last_error_message)

    def test_inline_mode_works_in_autocommit(self):
        with override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='sync')):
            transition_message = self.enqueue()
        self.widget.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(self.widget.status, 'submitted')
        self.assertEqual(self.widget.cb_log, 'callback,')

    def test_inline_mode_refuses_caller_transaction_before_enqueue(self):
        with override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='sync')):
            with transaction.atomic(), self.assertRaisesMessage(ImproperlyConfigured, 'caller transaction'):
                self.enqueue()
        self.assertFalse(TransitionMessage.objects.exists())
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')
        self.assertEqual(self.request_count(), 0)

    def test_pull_enqueue_inside_transaction_runs_after_commit(self):
        with transaction.atomic():
            transition_message = self.enqueue()
            self.assertFalse(transition_message.is_completed)
            self.assertEqual(self.request_count(), 0)
        self.assertTrue(run_once([QUEUE], isolate=True))
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'submitted')

    def test_different_model_database_is_refused_before_enqueue(self):
        self.widget._state.db = 'other'
        with self.assertRaisesMessage(ImproperlyConfigured, 'default PostgreSQL'):
            self.enqueue()
        self.assertFalse(TransitionMessage.objects.exists())
        self.assertEqual(self.request_count(), 0)

    def test_manual_state_change_during_provider_work_is_preserved(self):
        transition_message = self.enqueue()

        def manual_change(instance, **kwargs):
            Widget.objects.filter(pk=instance.pk).update(status='cancelled')

        with patch.object(ReceiptProcess.transitions[0].side_effects, '_commands', [submit_provider, manual_change]):
            run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.status, 'cancelled')
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertEqual(self.widget.cb_log, '')
        self.assertTrue(transition_message.is_completed)
        self.assertIn('[superseded]', transition_message.last_error_message)
        self.assertIn('Final state write skipped', transition_message.last_error_message)

    def test_database_error_after_confirmation_does_not_discard_receipt(self):
        transition_message = self.enqueue()

        def rejected_write(instance, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1 / 0')

        with patch.object(ReceiptProcess.transitions[0].side_effects, '_commands', [submit_provider, rejected_write]):
            run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertEqual(transition_message.errors_count, 1)
        self.assertFalse(transition_message.is_completed)
        run_background_transition(transition_message.pk)
        self.assertEqual(self.request_count(), 1)

    def test_rejected_failed_state_keeps_receipt_and_records_both_errors(self):
        transition_message = self.enqueue()
        original_save = Widget.save

        def refuse(instance, **kwargs):
            raise PermanentFailure('report failed permanently')

        def reject_failed_state(instance, *args, **kwargs):
            if instance.status == 'failed':
                raise ValueError('failed state rejected')
            return original_save(instance, *args, **kwargs)

        with patch.object(Widget, 'save', reject_failed_state), patch.object(
            ReceiptProcess.transitions[0].side_effects, '_commands', [submit_provider, refuse],
        ):
            run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertEqual(self.widget.status, 'submitting')
        self.assertEqual(self.widget.cb_log, 'callback,')
        self.assertTrue(transition_message.is_completed)
        self.assertTrue(transition_message.ended_in_failure)
        self.assertIn('report failed permanently', transition_message.last_error_message)
        self.assertIn('failed state rejected', transition_message.failure_side_effect_error)

    def test_lost_job_lock_does_not_allow_later_side_effect_and_retry_reuses_receipt(self):
        from contextlib import contextmanager
        from django_logic.background import non_atomic

        transition_message = self.enqueue()
        original_connection = non_atomic._job_connection
        lock_aliases = []
        later_calls = []

        @contextmanager
        def remember_connection():
            with original_connection() as alias:
                lock_aliases.append(alias)
                yield alias

        def lose_lock(instance, **kwargs):
            with connections[lock_aliases[-1]].cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                backend_pid = cursor.fetchone()[0]
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_terminate_backend(%s)', [backend_pid])
                self.assertTrue(cursor.fetchone()[0])

        def later_operation(instance, **kwargs):
            later_calls.append(True)

        # The drivers report a terminated connection with different disconnection exceptions.
        with patch.object(non_atomic, '_job_connection', remember_connection), patch.object(
            ReceiptProcess.transitions[0].side_effects, '_commands',
            [submit_provider, lose_lock, later_operation],
        ), self.assertRaises((InterfaceError, OperationalError)):
            run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertEqual(self.widget.status, 'submitting')
        self.assertFalse(transition_message.is_completed)
        self.assertEqual(later_calls, [])
        run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'submitted')
        self.assertEqual(self.request_count(), 1)
        self.assertFalse(any(alias.startswith('_django_logic_job_') for alias in vars(connections._connections)))

    def test_model_read_router_to_other_database_is_refused(self):
        class ReadRouter:
            def db_for_read(self, model, **hints):
                return 'other' if model is Widget else None

        with override_settings(DATABASE_ROUTERS=[ReadRouter()]), self.assertRaisesMessage(
            ImproperlyConfigured, 'default PostgreSQL',
        ):
            self.enqueue()
        self.assertFalse(TransitionMessage.objects.exists())
        self.assertEqual(self.request_count(), 0)

    def test_job_write_router_to_other_database_is_refused(self):
        class JobRouter:
            def db_for_write(self, model, **hints):
                return 'other' if model is TransitionMessage else None

        with override_settings(DATABASE_ROUTERS=[JobRouter()]), self.assertRaisesMessage(
            ImproperlyConfigured, 'default PostgreSQL',
        ):
            self.enqueue()
        self.assertFalse(TransitionMessage.objects.exists())
        self.assertEqual(self.request_count(), 0)

    def test_explicit_default_router_keeps_final_state_and_completion_on_lock_connection(self):
        class DefaultRouter:
            def db_for_read(self, model, **hints):
                return 'default'

            def db_for_write(self, model, **hints):
                return 'default'

        with override_settings(DATABASE_ROUTERS=[DefaultRouter()]):
            transition_message = self.enqueue()
            with patch.object(TransitionMessage, 'mark_as_completed', side_effect=ValueError('completion failed')):
                run_background_transition(transition_message.pk)
        self.widget.refresh_from_db()
        transition_message.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertEqual(self.widget.status, 'submitting')
        self.assertFalse(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 1)


    def test_temporary_job_connection_closes_when_default_database_uses_native_pooling(self):
        from django_logic.background.non_atomic import _job_connection

        default_connection = connections['default']
        pools = getattr(type(default_connection), '_connection_pools', {})
        original_pools = set(pools)
        with patch.dict(default_connection.settings_dict['OPTIONS'], {'pool': True}):
            with _job_connection() as alias:
                with connections[alias].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    backend_pid = cursor.fetchone()[0]
                self.assertTrue(default_connection.settings_dict['OPTIONS']['pool'])
            self.assertEqual(set(pools), original_pools)
        with connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM pg_stat_activity WHERE pid = %s', [backend_pid])
            self.assertEqual(cursor.fetchone()[0], 0)
