"""Local state changes recover after a killed process without a cache timeout."""
import os
from contextvars import copy_context
from pathlib import Path
import signal
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, connections, IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.core.exceptions import ImproperlyConfigured

from django_logic import Process, ProcessManager, Transition
from django_logic import DatabaseTransition
from django_logic.background import BackgroundEffect, BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import run_once, run_worker
from django_logic.background.runner import run_background_transition
from django_logic.exceptions import RefusalReason, TransitionNotAllowed, TransitionTemporarilyUnavailable
from django_logic.state import State
from tests import unbind_model_process
from tests.stability.base import requires_postgres, requires_real_redis
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
        DatabaseTransition('finish', sources=['draft'], target='done', side_effects=[save_local_work]),
        DatabaseTransition('observe', sources=['done']),
        BackgroundTransition('pending', sources=['draft'], queue='database-test'),
        DatabaseTransition('reenter', sources=['draft'], target='done', side_effects=[reenter]),
        DatabaseTransition('reenter_cached', sources=['draft'], target='done', side_effects=[reenter_cached]),
        DatabaseTransition('fail', sources=['draft'], target='done', failed_state='failed',
                           side_effects=[reject_database_write]),
    ]


def simulate_external_call(instance, **kwargs):
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_backend_pid()')
        EXTERNAL_WAIT[2].append(cursor.fetchone()[0])
    EXTERNAL_WAIT[0].set()
    if not EXTERNAL_WAIT[1].wait(5):
        raise TimeoutError('External test did not release the first call.')


class CachedProcess(Process):
    process_name = 'cached_process'
    transitions = [Transition('finish', sources=['draft'], target='done', side_effects=[save_local_work]),
                   Transition('external', sources=['draft'], target='done', side_effects=[simulate_external_call]),
                   Transition('finish_next', sources=['draft'], target='done', next_transition='pending_done'),
                   BackgroundTransition('pending_done', sources=['done'], queue='database-followup')]


class CompletionProcess(Process):
    process_name = 'completion_process'
    transitions = [BackgroundTransition('complete', sources=['draft'], queue='database-test',
                   background_effects=[BackgroundEffect('local_completion', run_local_completion)])]


@requires_postgres
@requires_real_redis
@override_settings(DJANGO_LOGIC={'BACKGROUND_EXECUTION': 'pull', 'LOCK_TIMEOUT': 7200})
class DatabaseTransitionTests(TransactionTestCase):
    """Real commits, processes and independent connections exercise lock ownership."""
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
        parent = TransitionMessage.objects.get(instance_id=str(outer.pk), process_name='completion_process')
        run_background_transition(parent.pk)
        job = TransitionMessage.objects.get(parent_job_id=parent.pk)
        self.assertTrue(run_once(['database-test'], isolate=True))
        job.refresh_from_db()
        self.assertEqual(job.errors_count, 1)
        for row in (outer, nested):
            row.refresh_from_db()
            self.assertEqual((row.status, row.side_effect_log), ('draft', ''))
            self.assertFalse(State(row, 'status').is_locked())
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
            self.assertFalse(State(row, 'status').is_locked())
            row.refresh_from_db()
            self.assertEqual(row.status, 'draft')
            row.database_process.finish()
            row.refresh_from_db()
            self.assertEqual(row.status, 'done')

    def test_killed_nested_background_submission_rolls_back_and_is_saved_once(self):
        outer, nested = Order.objects.create(), Order.objects.create()
        outer.completion_process.complete(nested_id=nested.pk, enqueue=True)
        parent = TransitionMessage.objects.get(instance_id=str(outer.pk), effect_name='')
        run_background_transition(parent.pk)
        job = TransitionMessage.objects.get(parent_job_id=parent.pk)
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
        for row in (outer, nested):
            row.refresh_from_db()
            self.assertEqual(row.status, 'draft')
            self.assertFalse(State(row, 'status').is_locked())
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        self.assertEqual(TransitionMessage.objects.filter(transition_name='pending_done').count(), 1)
        for row in (outer, nested):
            row.refresh_from_db()
            self.assertEqual(row.status, 'done')

    def test_failed_state_survives_database_failure(self):
        row = Order.objects.create()
        with self.assertRaises(IntegrityError):
            row.database_process.fail()
        row.refresh_from_db()
        self.assertEqual(row.status, 'failed')
        self.assertFalse(State(row, 'status').is_locked())

    def test_existing_cached_lock_is_not_removed(self):
        row = Order.objects.create()
        held = State(row, 'status')
        self.assertTrue(held.lock())
        self.addCleanup(held.unlock)
        with self.assertRaises(TransitionNotAllowed) as error:
            row.database_process.finish()
        self.assertEqual(error.exception.reason, RefusalReason.LOCKED)
        self.assertTrue(held.is_locked())

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
            self.assertIsNone(cache.get(State(row, 'status')._get_hash()))
            second = pool.submit(call)
            time.sleep(0.1)
            self.assertFalse(second.done())
            release.set()
            self.assertIsNone(first.result(timeout=5))
            self.assertEqual(second.result(timeout=5), RefusalReason.SOURCE_STATE)
        row.refresh_from_db()
        self.assertEqual(row.side_effect_log, 'saved,')

    def test_external_call_stays_excluded_after_its_database_connection_dies(self):
        row = Order.objects.create()
        started, release, backend_ids = Event(), Event(), []
        def external():
            try:
                Order.objects.get(pk=row.pk).cached_process.external()
            except Exception as error:
                return type(error).__name__
            finally:
                connections.close_all()
        with patch(__name__ + '.EXTERNAL_WAIT', (started, release, backend_ids)), ThreadPoolExecutor(1) as pool:
            first = pool.submit(external)
            try:
                self.assertTrue(started.wait(5))
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_terminate_backend(%s)', [backend_ids[0]])
                    self.assertTrue(cursor.fetchone()[0])
                for process in (row.database_process, row.cached_process):
                    with self.assertRaises(TransitionNotAllowed) as error:
                        process.finish()
                    self.assertEqual(error.exception.reason, RefusalReason.LOCKED)
                self.assertTrue(State(row, 'status').is_locked())
                self.assertEqual(len(backend_ids), 1)
            finally:
                release.set()
            self.assertIsNotNone(first.result(timeout=5))
        self.assertFalse(State(row, 'status').is_locked())

    def test_whole_worker_kill_leaves_no_lock_and_next_worker_completes(self):
        row = Order.objects.create()
        marker = str(Path(self.directory.name) / 'whole-worker')
        row.completion_process.complete(marker=marker, wait=True)
        parent = TransitionMessage.objects.get(instance_id=str(row.pk), effect_name='')
        run_background_transition(parent.pk)
        job = TransitionMessage.objects.get(parent_job_id=parent.pk)
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
        self.assertFalse(State(row, 'status').is_locked())
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
        self.assertFalse(State(row, 'status').is_locked())

    def test_base_exception_does_not_leave_a_reentry_guard(self):
        row = Order.objects.create()
        with patch.object(Order, 'save', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            row.database_process.finish()
        row.refresh_from_db()
        self.assertFalse(State(row, 'status').is_locked())
        row.database_process.finish()
        row.refresh_from_db()
        self.assertEqual(row.status, 'done')

    def test_ordinary_call_after_exception_still_acquires_its_cached_lock(self):
        row = Order.objects.create()
        with patch.object(Order, 'save', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            row.database_process.finish()
        row.refresh_from_db()
        observed = []
        original = Order.save

        def inspect_lock(current, *args, **kwargs):
            observed.append(cache.get(State(current, 'status')._get_hash()) is not None)
            return original(current, *args, **kwargs)

        with patch.object(Order, 'save', inspect_lock):
            row.cached_process.finish()
        self.assertTrue(observed)
        self.assertTrue(all(observed))
        self.assertFalse(State(row, 'status').is_locked())

    def test_a_copied_context_does_not_retain_a_finished_calls_guard(self):
        row = Order.objects.create()
        contexts = []
        original = Order.save
        def capture(instance, *args, **kwargs):
            contexts.append(copy_context())
            return original(instance, *args, **kwargs)
        with patch.object(Order, 'save', capture):
            row.database_process.finish()
        self.assertTrue(contexts)
        self.assertIsNotNone(contexts[0].run(row.database_process.observe))

    def test_commit_callback_does_not_inherit_database_only_locking(self):
        local, ordinary = Order.objects.create(), Order.objects.create()
        observed = []
        original = Order.save

        def inspect_commit(current, *args, **kwargs):
            result = original(current, *args, **kwargs)
            if current.pk == local.pk and kwargs.get('update_fields') == ['status']:
                transaction.on_commit(ordinary.cached_process.finish)
            if current.pk == ordinary.pk:
                observed.append(cache.get(State(current, 'status')._get_hash()) is not None)
            return result

        with patch.object(Order, 'save', inspect_commit):
            local.database_process.finish()
        self.assertTrue(observed)
        self.assertTrue(all(observed))
        ordinary.refresh_from_db()
        self.assertEqual(ordinary.status, 'done')


class DatabaseTransitionDeclarationTests(SimpleTestCase):
    def test_lock_false_is_not_a_database_transition(self):
        with self.assertRaises(ImproperlyConfigured):
            DatabaseTransition('change', sources=[], lock=False)


class DatabaseTransitionBackendTests(TestCase):
    def test_non_postgresql_backend_is_refused_before_side_effects(self):
        if connection.vendor == 'postgresql':
            self.skipTest('This case exercises an actual non-PostgreSQL backend.')
        row = Order.objects.create()
        transition = DatabaseTransition('change', sources=['draft'], target='done', side_effects=[save_local_work])
        with self.assertRaisesMessage(ImproperlyConfigured, 'requires PostgreSQL'):
            transition.change_state(State(row, 'status'))
        row.refresh_from_db()
        self.assertEqual((row.status, row.side_effect_log), ('draft', ''))
