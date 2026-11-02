"""Job history distinguishes initial waiting, retries and provider deferrals."""
import os
import threading
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process, Transition
from django_logic.background import BackgroundTransition, Defer
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import run_once
from django_logic.background.runner import run_background_transition
from tests import dl_settings
from tests.background.models import Widget, bg_ok
from tests.background.test_pull_workers import _hold_row_lock
from tests.stability.base import requires_postgres


QUEUE = 'attempt_history'
SETTINGS = dl_settings(BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_RETRY_MINUTES=0)


def defer_provider(instance, **kwargs):
    bg_ok(instance)
    raise Defer(until=datetime.now(datetime_timezone.utc) + timedelta(minutes=1), reason='provider allowance')


def reject_provider(instance, **kwargs):
    raise ValueError('provider rejected the request')


def defer_once(instance, **kwargs):
    if instance.se_log == 'confirmed':
        return
    instance.se_log = 'confirmed'
    instance.save(update_fields=['se_log'])
    raise Defer(until=timezone.now() + timedelta(minutes=1), reason='next portion')


def crash_after_receipt(instance, **kwargs):
    instance.se_log = 'confirmed'
    instance.save(update_fields=['se_log'])
    os._exit(3)


class HistoryProcess(Process):
    process_name = 'attempt_history'
    transitions = [
        BackgroundTransition('submit', sources=['draft'], target='done',
                             side_effects=[defer_provider], queue=QUEUE),
    ]


class ReceiptHistoryProcess(Process):
    process_name = 'receipt_history'
    transitions = [
        BackgroundTransition('submit', sources=['draft'], target='done',
                             side_effects=[defer_once], queue=QUEUE, atomic=False),
    ]


class LoggedFailureProcess(Process):
    process_name = 'logged_failure'
    transitions = [
        Transition('submit', sources=['draft'], target='done',
                   side_effects=[bg_ok, reject_provider]),
    ]


@override_settings(DJANGO_LOGIC=SETTINGS)
class AttemptHistoryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.widget = Widget.objects.create()

    def enqueue(self):
        HistoryProcess(field_name='status', instance=self.widget).submit()
        return TransitionMessage.objects.get()

    def allow_retry(self, row):
        TransitionMessage.objects.filter(pk=row.pk).update(eligible_at=timezone.now() - timedelta(seconds=1))

    def test_multiple_deferrals_retain_first_start_and_history_after_success(self):
        row = self.enqueue()
        self.assertEqual((row.start_attempt_count, row.deferral_count), (0, 0))
        self.assertIsNone(row.first_started_at)
        run_background_transition(row.pk)
        row.refresh_from_db()
        first_start = row.first_started_at
        self.assertEqual(first_start, row.started_at)
        self.assertEqual((row.start_attempt_count, row.deferral_count, row.errors_count), (1, 1, 0))
        self.assertEqual(row.last_deferred_until, row.eligible_at)
        self.assertGreaterEqual(row.last_deferred_at, first_start)
        self.assertEqual(row.last_deferral_reason, 'provider allowance')
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, '', 'the failed atomic attempt must roll back')

        run_background_transition(row.pk)
        row.refresh_from_db()
        self.assertEqual((row.start_attempt_count, row.deferral_count), (1, 1), 'a future wait must not start')
        self.allow_retry(row)
        run_background_transition(row.pk)
        row.refresh_from_db()
        last_wait = (row.last_deferred_at, row.last_deferred_until, row.last_deferral_reason)
        self.assertEqual((row.start_attempt_count, row.deferral_count), (2, 2))
        self.assertEqual(row.first_started_at, first_start)
        self.assertGreaterEqual(row.started_at, first_start)

        self.allow_retry(row)
        with patch.object(HistoryProcess.transitions[0].side_effects, '_commands', [bg_ok]):
            run_background_transition(row.pk)
        row.refresh_from_db()
        self.assertTrue(row.is_completed)
        self.assertEqual((row.start_attempt_count, row.deferral_count, row.errors_count), (3, 2, 0))
        self.assertEqual(row.first_started_at, first_start)
        self.assertIsNone(row.eligible_at)
        self.assertEqual(row.deferral_reason, '')
        self.assertEqual((row.last_deferred_at, row.last_deferred_until, row.last_deferral_reason), last_wait)
        run_background_transition(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.start_attempt_count, 3, 'a completed job must not start again')

    def test_error_after_deferral_keeps_wait_history(self):
        row = self.enqueue()
        run_background_transition(row.pk)
        row.refresh_from_db()
        first_start = row.first_started_at
        last_wait = row.last_deferred_until
        self.allow_retry(row)
        with patch.object(HistoryProcess.transitions[0].side_effects, '_commands', [reject_provider]):
            run_background_transition(row.pk)
        row.refresh_from_db()
        self.assertEqual((row.start_attempt_count, row.deferral_count, row.errors_count), (2, 1, 1))
        self.assertEqual(row.first_started_at, first_start)
        self.assertIsNone(row.eligible_at)
        self.assertEqual(row.deferral_reason, '')
        self.assertEqual(row.last_deferred_until, last_wait)
        self.assertEqual(row.last_deferral_reason, 'provider allowance')

    @override_settings(USE_TZ=False, TIME_ZONE='Asia/Hong_Kong')
    def test_retained_wait_uses_the_database_timezone(self):
        row = self.enqueue()
        run_background_transition(row.pk)
        row.refresh_from_db()
        self.assertTrue(timezone.is_naive(row.first_started_at))
        self.assertTrue(timezone.is_naive(row.last_deferred_at))
        self.assertTrue(timezone.is_naive(row.last_deferred_until))
        self.assertEqual(row.last_deferred_until, row.eligible_at)
        self.assertAlmostEqual((row.last_deferred_until - row.last_deferred_at).total_seconds(), 60, delta=5)

    def test_old_started_at_does_not_fabricate_first_attempt_history(self):
        row = self.enqueue()
        old_start = timezone.now() - timedelta(days=1)
        TransitionMessage.objects.filter(pk=row.pk).update(started_at=old_start)
        run_background_transition(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.first_started_at, row.started_at)
        self.assertGreater(row.first_started_at, old_start)
        self.assertEqual(row.start_attempt_count, 1)

    def test_earlier_producer_can_omit_new_columns(self):
        row = self.enqueue()
        row.is_completed = True
        row.save(update_fields=['is_completed'])
        new_fields = {'id', 'start_attempt_count', 'deferral_count', 'first_started_at',
                      'last_deferral_reason', 'last_deferred_at', 'last_deferred_until'}
        columns = ', '.join(connection.ops.quote_name(field.column)
                            for field in TransitionMessage._meta.concrete_fields if field.name not in new_fields)
        table = connection.ops.quote_name(TransitionMessage._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f'INSERT INTO {table} ({columns}) SELECT {columns} FROM {table} WHERE id = %s', [row.pk])
        copied = TransitionMessage.objects.exclude(pk=row.pk).get()
        self.assertEqual((copied.start_attempt_count, copied.deferral_count, copied.last_deferral_reason), (0, 0, ''))
        self.assertIsNone(copied.first_started_at)
        self.assertIsNone(copied.last_deferred_at)
        self.assertIsNone(copied.last_deferred_until)

    def test_essential_error_identifies_operation_without_arguments(self):
        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            with self.assertRaises(ValueError):
                LoggedFailureProcess(field_name='status', instance=self.widget).submit(private_input='do-not-log-this')
        messages = '\n'.join(logs.output)
        self.assertIn('SideEffect reject_provider failed action=submit', messages)
        self.assertIn(f'instance=bg_tests_widget-status-{self.widget.pk}', messages)
        self.assertIn('ValueError: provider rejected the request', messages)
        self.assertNotIn('do-not-log-this', messages)


@requires_postgres
@override_settings(DJANGO_LOGIC=SETTINGS)
class AttemptHistoryPostgresTests(TransactionTestCase):
    databases = '__all__'

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.widget = Widget.objects.create()

    def test_locked_job_does_not_count_a_start(self):
        HistoryProcess(field_name='status', instance=self.widget).submit()
        row = TransitionMessage.objects.get()
        locked, release = threading.Event(), threading.Event()
        thread = threading.Thread(target=_hold_row_lock, args=(row.pk, locked, release), daemon=True)
        thread.start()
        try:
            self.assertTrue(locked.wait(timeout=10))
            run_background_transition(row.pk)
            row.refresh_from_db()
            self.assertIsNone(row.first_started_at)
            self.assertEqual(row.start_attempt_count, 0)
        finally:
            release.set()
            thread.join(timeout=10)
        run_background_transition(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.start_attempt_count, 1)

    def test_simultaneous_start_markers_do_not_imply_duplicate_execution(self):
        from django_logic.background import runner

        HistoryProcess(field_name='status', instance=self.widget).submit()
        row = TransitionMessage.objects.get()
        recorded, release = threading.Event(), threading.Event()
        original_run = runner._run_atomic
        failures = []

        def pause_after_start(message_id):
            if threading.current_thread() is first_worker:
                recorded.set()
                if not release.wait(timeout=10):
                    raise AssertionError('The other worker did not finish.')
            return original_run(message_id)

        def first_start():
            try:
                run_background_transition(row.pk)
            except BaseException as error:
                failures.append(error)
            finally:
                connections.close_all()

        first_worker = threading.Thread(target=first_start, daemon=True)
        with patch.object(runner, '_run_atomic', side_effect=pause_after_start), \
                patch.object(HistoryProcess.transitions[0].side_effects, '_commands', [bg_ok]):
            first_worker.start()
            try:
                self.assertTrue(recorded.wait(timeout=10))
                row.refresh_from_db()
                first_start_time = row.first_started_at
                self.assertEqual(row.start_attempt_count, 1)
                run_background_transition(row.pk)
            finally:
                release.set()
                first_worker.join(timeout=10)
        self.assertFalse(first_worker.is_alive())
        self.assertEqual(failures, [])
        row.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertTrue(row.is_completed)
        self.assertEqual(row.start_attempt_count, 2)
        self.assertEqual(row.first_started_at, first_start_time)
        self.assertEqual(self.widget.se_log, 'ok,')

    def test_separate_job_processes_retain_progress_and_deferral_history(self):
        ReceiptHistoryProcess(field_name='status', instance=self.widget).submit()
        row = TransitionMessage.objects.get()
        self.assertTrue(run_once([QUEUE], isolate=True))
        row.refresh_from_db()
        first_start = row.first_started_at
        wait_until = row.last_deferred_until
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertFalse(row.is_completed)
        self.assertEqual((row.start_attempt_count, row.deferral_count, row.errors_count), (1, 1, 0))
        TransitionMessage.objects.filter(pk=row.pk).update(eligible_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(run_once([QUEUE], isolate=True))
        row.refresh_from_db()
        self.assertTrue(row.is_completed)
        self.assertEqual((row.start_attempt_count, row.deferral_count, row.errors_count), (2, 1, 0))
        self.assertEqual(row.first_started_at, first_start)
        self.assertEqual(row.last_deferred_until, wait_until)
        self.assertEqual(row.last_deferral_reason, 'next portion')

    def test_crashed_job_process_preserves_first_start_and_count(self):
        ReceiptHistoryProcess(field_name='status', instance=self.widget).submit()
        row = TransitionMessage.objects.get()
        with patch.object(ReceiptHistoryProcess.transitions[0].side_effects, '_commands', [crash_after_receipt]):
            self.assertTrue(run_once([QUEUE], isolate=True))
        row.refresh_from_db()
        first_start = row.first_started_at
        self.assertIsNotNone(first_start)
        self.assertEqual((row.start_attempt_count, row.deferral_count, row.errors_count), (1, 0, 1))
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.se_log, 'confirmed')
        self.assertFalse(row.is_completed)
        self.assertTrue(run_once([QUEUE], isolate=True))
        row.refresh_from_db()
        self.assertTrue(row.is_completed)
        self.assertEqual((row.start_attempt_count, row.deferral_count, row.errors_count), (2, 0, 1))
        self.assertEqual(row.first_started_at, first_start)
