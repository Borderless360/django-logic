"""Local state changes recover after a killed process without a cache timeout."""
import os
from datetime import timedelta
from pathlib import Path
import signal
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, connections, IntegrityError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import run_once, run_worker
from django_logic.background.runner import run_background_transition
from django_logic.exceptions import RefusalReason, TransitionNotAllowed, TransitionTemporarilyUnavailable
from tests import unbind_model_process
from tests.stability.base import requires_postgres
from tests.stability.models import Order


LOCAL_WAIT = None
EXTERNAL_WAIT = None


def save_local_work(instance, marker='', nested_id=None, wait=False, enqueue=False, **kwargs):
    instance.side_effect_log += 'saved,'
    instance.save(update_fields=['side_effect_log'])
    if LOCAL_WAIT:
        LOCAL_WAIT[0].set()
        if not LOCAL_WAIT[1].wait(5):
            raise TimeoutError('Concurrent test did not release the first call.')
    if nested_id and enqueue:
        Order.objects.get(pk=nested_id).cached_process.finish_next()
    elif nested_id:
        Order.objects.get(pk=nested_id).database_process.finish(marker=marker, wait=wait)
    elif marker and not Path(marker).exists():
        Path(marker).touch()
        if wait:
            time.sleep(30)
        else:
            os.kill(os.getpid(), signal.SIGKILL)


def run_local_completion(instance, **kwargs):
    instance.database_process.finish(**{key: kwargs[key] for key in ('marker', 'nested_id', 'wait', 'enqueue') if key in kwargs})


def reenter(instance, **kwargs):
    instance.database_process.finish()


def reenter_cached(instance, **kwargs):
    instance.cached_process.finish()


def reject_database_write(instance, **kwargs):
    Order.objects.create(pk=instance.pk)


class DatabaseProcess(Process):
    process_name = 'database_process'
    transitions = [
        Transition('finish', sources=['draft'], target='done', side_effects=[save_local_work]),
        BackgroundTransition('pending', sources=['draft'], queue='database-test'),
        Transition('reenter', sources=['draft'], target='done', side_effects=[reenter]),
        Transition('reenter_cached', sources=['draft'], target='done', side_effects=[reenter_cached]),
        Transition('fail', sources=['draft'], target='done', failed_state='failed',
                           side_effects=[reject_database_write]),
    ]


def simulate_external_call(instance, **kwargs):
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_backend_pid()')
        EXTERNAL_WAIT[2].append(cursor.fetchone()[0])
    EXTERNAL_WAIT[0].set()
    if not EXTERNAL_WAIT[1].wait(5):
        raise TimeoutError('External test did not release the first call.')


def submit_optional_pending_job(instance, **kwargs):
    instance.cached_process.pending_done(**kwargs)


class CachedProcess(Process):
    process_name = 'cached_process'
    transitions = [Transition('finish', sources=['draft'], target='done', side_effects=[save_local_work]),
                   Transition('external', sources=['draft'], target='done', side_effects=[simulate_external_call]),
                   Transition('finish_next', sources=['draft'], target='done', callbacks=[submit_optional_pending_job]),
                   BackgroundTransition('pending_done', sources=['done'], queue='database-followup')]


def submit_job_for_another_record(instance, nested_id, **kwargs):
    Order.objects.get(pk=nested_id).database_process.pending()


class CompletionProcess(Process):
    process_name = 'completion_process'
    transitions = [BackgroundTransition('complete', sources=['draft', 'done'], queue='database-test',
                                        side_effects=[run_local_completion]),
                   BackgroundTransition('submit', sources=['draft'], queue='database-test',
                                        side_effects=[submit_job_for_another_record])]


@requires_postgres
@override_settings(DJANGO_LOGIC={'BACKGROUND_EXECUTION': 'pull'})
class DatabaseTransitionTests(TransactionTestCase):
    """Real commits and independent processes exercise transaction rollback and crash recovery."""
    def setUp(self):
        cache.clear()
        for process in (DatabaseProcess, CachedProcess, CompletionProcess):
            ProcessManager.bind_model_process(Order, process, state_field='status')
            self.addCleanup(unbind_model_process, Order, process)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_killed_nested_completion_retries_without_waiting_for_cache_expiry(self):
        outer, nested = Order.objects.create(), Order.objects.create()
        marker = str(Path(self.directory.name) / 'killed')
        outer.completion_process.complete(marker=marker, nested_id=nested.pk)
        job = TransitionMessage.objects.get(instance_id=str(outer.pk), process_name='completion_process')
        self.assertTrue(run_once(['database-test'], isolate=True))
        job.refresh_from_db()
        self.assertEqual(job.errors_count, 1)
        for row in (outer, nested):
            row.refresh_from_db()
            self.assertEqual((row.status, row.side_effect_log), ('draft', ''))
        # Exercise the saved attempt directly; its normal retry delay is unchanged.
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        for row in (outer, nested):
            row.refresh_from_db()
            self.assertEqual((row.status, row.side_effect_log), ('done', 'saved,'))

    def test_reentry_is_refused_for_database_and_cached_transitions(self):
        for action in ('reenter', 'reenter_cached'):
            row = Order.objects.create()
            with self.subTest(action=action), self.assertRaises(TransitionNotAllowed) as error:
                getattr(row.database_process, action)()
            self.assertEqual(error.exception.reason, RefusalReason.LOCKED)
            row.refresh_from_db()
            self.assertEqual(row.status, 'draft')
            row.database_process.finish()
            row.refresh_from_db()
            self.assertEqual(row.status, 'done')

    def test_killed_optional_callback_does_not_reopen_committed_work(self):
        outer, nested = Order.objects.create(), Order.objects.create()
        outer.completion_process.complete(nested_id=nested.pk, enqueue=True)
        job = TransitionMessage.objects.get(instance_id=str(outer.pk), process_name='completion_process')
        marker = Path(self.directory.name) / 'enqueue-killed'
        original = TransitionMessage.save

        def kill_after_job_save(current, *args, **kwargs):
            result = original(current, *args, **kwargs)
            if current.transition_name == 'pending_done' and not marker.exists():
                marker.touch()
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        with patch.object(TransitionMessage, 'save', kill_after_job_save):
            self.assertTrue(run_once(['database-test'], isolate=True))
        self.assertTrue(marker.exists())
        self.assertFalse(TransitionMessage.objects.filter(transition_name='pending_done').exists())
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        for row in (outer, nested):
            row.refresh_from_db()
            self.assertEqual(row.status, 'done')

    def test_job_killed_while_it_saves_another_job_leaves_no_cache_lock(self):
        outer, nested = Order.objects.create(), Order.objects.create()
        outer.completion_process.submit(nested_id=nested.pk)
        job = TransitionMessage.objects.get(instance_id=str(outer.pk), transition_name='submit')
        marker = Path(self.directory.name) / 'submission-killed'
        original = TransitionMessage.save

        def kill_after_job_save(current, *args, **kwargs):
            result = original(current, *args, **kwargs)
            if current.transition_name == 'pending' and not marker.exists():
                marker.touch()
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        with patch.object(TransitionMessage, 'save', kill_after_job_save):
            self.assertTrue(run_once(['database-test'], isolate=True))
        self.assertTrue(marker.exists())
        self.assertFalse(TransitionMessage.objects.filter(transition_name='pending').exists())
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertTrue(job.is_completed, job.last_error_message)
        self.assertFalse(job.ended_in_failure)
        self.assertEqual(TransitionMessage.objects.filter(transition_name='pending').count(), 1)

    def test_failed_state_survives_database_failure(self):
        row = Order.objects.create()
        with self.assertRaises(IntegrityError):
            row.database_process.fail()
        row.refresh_from_db()
        self.assertEqual(row.status, 'failed')


    def test_concurrent_database_calls_wait_for_commit_then_recheck_source(self):
        row = Order.objects.create()
        started, release = Event(), Event()
        def call():
            try:
                Order.objects.get(pk=row.pk).database_process.finish()
            except TransitionNotAllowed as error:
                return error.reason
            finally:
                connections.close_all()
        with patch(__name__ + '.LOCAL_WAIT', (started, release)), ThreadPoolExecutor(2) as pool:
            first = pool.submit(call)
            self.assertTrue(started.wait(5))
            second = pool.submit(call)
            time.sleep(0.1)
            self.assertFalse(second.done())
            release.set()
            self.assertIsNone(first.result(timeout=5))
            self.assertEqual(second.result(timeout=5), RefusalReason.SOURCE_STATE)
        row.refresh_from_db()
        self.assertEqual(row.side_effect_log, 'saved,')


    def test_whole_worker_kill_leaves_no_lock_and_next_worker_completes(self):
        row = Order.objects.create()
        marker = str(Path(self.directory.name) / 'whole-worker')
        row.completion_process.complete(marker=marker, wait=True)
        job = TransitionMessage.objects.get(instance_id=str(row.pk), process_name='completion_process')
        connections.close_all()
        worker_pid = os.fork()
        if worker_pid == 0:
            os.setsid()
            try:
                run_worker(['database-test'], concurrency=1)
            finally:
                os._exit(1)
        try:
            deadline = time.monotonic() + 10
            while not Path(marker).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(Path(marker).exists())
        finally:
            os.killpg(worker_pid, signal.SIGKILL)
            os.waitpid(worker_pid, 0)
        TransitionMessage.objects.filter(pk=job.pk).update(started_at=timezone.now() - timedelta(seconds=6))
        self.assertTrue(run_once(['database-test'], isolate=True))
        job.refresh_from_db()
        row.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        self.assertEqual((row.status, row.side_effect_log), ('done', 'saved,'))

    def test_pending_background_job_still_refuses_the_local_state_change(self):
        row = Order.objects.create()
        row.database_process.pending()
        with self.assertRaises(TransitionTemporarilyUnavailable) as error:
            row.database_process.finish()
        self.assertEqual(error.exception.reason, RefusalReason.BACKGROUND_IN_FLIGHT)
        row.refresh_from_db()
        self.assertEqual((row.status, row.side_effect_log), ('draft', ''))

    def test_base_exception_does_not_leave_a_reentry_guard(self):
        row = Order.objects.create()
        with patch.object(Order, 'save', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            row.database_process.finish()
        row.refresh_from_db()
        row.database_process.finish()
        row.refresh_from_db()
        self.assertEqual(row.status, 'done')
