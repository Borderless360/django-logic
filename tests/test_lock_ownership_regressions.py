"""Lock ownership remains accurate across connection loss and stopped jobs."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import signal
import tempfile
from threading import Event
import time
from unittest.mock import patch
from uuid import uuid4

from django.db import OperationalError, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from django_logic import DatabaseTransition, Process, Transition
from django_logic import lock_ownership
from django_logic.background import BackgroundTransition, pull
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import TransitionNotAllowed
from django_logic.models import JobProcessOwner, StateLock
from django_logic.state import State
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres


class VisibleLockProcess(Process):
    transitions = [Transition('finish', sources=['draft'], target='finished')]


def independent_call(function, *args):
    try:
        return function(*args)
    finally:
        connections.close_all()


@requires_postgres
class CommittedLockRegressionTests(TransactionTestCase):
    def start_call(self, row, entered, release, pids, calls):
        def work(instance, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                pids.append(cursor.fetchone()[0])
            calls.append('first')
            entered.set()
            if not release.wait(5):
                raise RuntimeError('The test did not release its first operation.')

        try:
            local = Widget.objects.get(pk=row.pk)
            Transition('finish', sources=['draft'], target='finished', side_effects=[work]).change_state(
                State(local, 'status', process_name='lock_review'))
            return 'completed'
        except Exception as error:
            return type(error).__name__
        finally:
            connections.close_all()

    def test_other_connections_see_the_lock_and_hide_unavailable_actions(self):
        row = Widget.objects.create(status='draft')
        entered, release = Event(), Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.start_call, row, entered, release, [], [])
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(State(row, 'status').is_locked())
                self.assertEqual(VisibleLockProcess(instance=row, field_name='status').get_available_actions(), [])
            finally:
                release.set()
                self.assertEqual(first.result(timeout=5), 'completed')
        self.assertFalse(State(row, 'status').is_locked())

    def test_lost_business_connection_still_refuses_overlapping_external_work(self):
        row = Widget.objects.create(status='draft')
        entered, release = Event(), Event()
        pids, calls = [], []
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.start_call, row, entered, release, pids, calls)
            try:
                self.assertTrue(entered.wait(3))
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_terminate_backend(%s)', [pids[0]])
                    self.assertTrue(cursor.fetchone()[0])
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT count(*) FROM pg_stat_activity WHERE pid=%s', [pids[0]])
                        if cursor.fetchone()[0] == 0:
                            break
                    time.sleep(0.01)
                else:
                    self.fail('The test business connection did not stop.')
                self.assertFalse(first.done(), 'The first external operation must still be running.')
                self.assertTrue(State(row, 'status').is_locked())
                second = Transition('finish', sources=['draft'], target='finished',
                                    side_effects=[lambda instance, **kwargs: calls.append('second')])
                with self.assertRaises(TransitionNotAllowed):
                    second.change_state(State(row, 'status', process_name='lock_review'))
                self.assertEqual(calls, ['first'])
            finally:
                release.set()
                self.assertEqual(first.result(timeout=5), 'OperationalError')
        self.assertFalse(State(row, 'status').is_locked())

    def test_business_rollback_does_not_undo_a_committed_lock(self):
        row = Widget.objects.create(status='draft')
        owner = State(row, 'status')
        try:
            with self.assertRaisesRegex(RuntimeError, 'business rollback'):
                with transaction.atomic():
                    self.assertTrue(owner.lock())
                    row.status = 'finished'
                    row.save(update_fields=['status'])
                    raise RuntimeError('business rollback')
            row.refresh_from_db()
            self.assertEqual(row.status, 'draft')
            self.assertTrue(owner.is_locked())
            self.assertFalse(State(row, 'status').lock())
        finally:
            owner.unlock()
        self.assertFalse(owner.is_locked())

    def test_business_rollback_does_not_restore_a_released_lock(self):
        row = Widget.objects.create(status='draft')
        owner = State(row, 'status')
        self.assertTrue(owner.lock())
        with self.assertRaisesRegex(RuntimeError, 'business rollback'):
            with transaction.atomic():
                owner.unlock()
                raise RuntimeError('business rollback')
        successor = State(row, 'status')
        self.assertTrue(successor.lock())
        successor.unlock()

    def wait_for_database_lock(self, pid):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            with connection.cursor() as cursor:
                cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', [pid])
                observed = cursor.fetchone()
            if observed and observed[0] == 'Lock':
                return
            time.sleep(0.01)
        self.fail('The concurrent operation did not wait for the expected database lock.')

    def retire(self, owner_id, entered=None, pids=None):
        with transaction.atomic():
            if pids is not None:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids.append(cursor.fetchone()[0])
            if entered is not None:
                entered.set()
            lock_ownership.retire_process_owner(owner_id)

    def test_acquisition_committing_before_cleanup_is_removed(self):
        owner_id = lock_ownership.create_process_owner()
        acquired, allow_commit, cleanup_started = Event(), Event(), Event()
        pids = []
        ordinary_connection = lock_ownership._lock_connection

        @contextmanager
        def hold_before_commit():
            with ordinary_connection() as alias:
                yield alias
                acquired.set()
                if not allow_commit.wait(5):
                    raise RuntimeError('The test did not release the lock acquisition.')

        with patch.object(lock_ownership, '_process_owner', (os.getpid(), owner_id, None)), \
                patch.object(lock_ownership, '_lock_connection', hold_before_commit), \
                ThreadPoolExecutor(max_workers=2) as pool:
            taking = pool.submit(independent_call, lock_ownership.acquire_lock, 'acquisition-first', 7200)
            try:
                self.assertTrue(acquired.wait(3))
                removing = pool.submit(independent_call, self.retire, owner_id, cleanup_started, pids)
                self.assertTrue(cleanup_started.wait(3))
                self.wait_for_database_lock(pids[0])
                self.assertFalse(taking.done(), 'Acquisition must not return before its transaction commits.')
            finally:
                allow_commit.set()
            self.assertIsNotNone(taking.result(timeout=5))
            removing.result(timeout=5)
        self.assertFalse(JobProcessOwner.objects.filter(pk=owner_id).exists())
        self.assertFalse(StateLock.objects.filter(key='acquisition-first').exists())

    def test_cleanup_committing_before_acquisition_refuses_business_work(self):
        owner_id = lock_ownership.create_process_owner()
        entered, pids = Event(), []
        ordinary_connection = lock_ownership._lock_connection

        @contextmanager
        def observe_connection():
            with ordinary_connection() as alias:
                with connections[alias].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids.append(cursor.fetchone()[0])
                entered.set()
                yield alias

        with patch.object(lock_ownership, '_process_owner', (os.getpid(), owner_id, None)), \
                patch.object(lock_ownership, '_lock_connection', observe_connection), \
                ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                lock_ownership.retire_process_owner(owner_id)
                taking = pool.submit(independent_call, lock_ownership.acquire_lock, 'cleanup-first', 7200)
                self.assertTrue(entered.wait(3))
                self.wait_for_database_lock(pids[0])
            with self.assertRaises(lock_ownership.JobProcessStopped):
                taking.result(timeout=5)
        self.assertFalse(StateLock.objects.filter(key='cleanup-first').exists())

    def test_delayed_owner_cleanup_and_old_token_preserve_a_new_owner(self):
        old_owner = lock_ownership.create_process_owner()
        new_owner = lock_ownership.create_process_owner()
        key = 'replacement'
        with patch.object(lock_ownership, '_process_owner', (os.getpid(), old_owner, None)):
            old_token = lock_ownership.acquire_lock(key, 7200)
        lock_ownership.release_lock(key, old_token)
        with patch.object(lock_ownership, '_process_owner', (os.getpid(), new_owner, None)):
            new_token = lock_ownership.acquire_lock(key, 7200)
        self.assertIsNotNone(new_token)
        lock_ownership.release_lock(key, old_token)
        self.retire(old_owner)
        current = StateLock.objects.get(key=key)
        self.assertEqual((current.token, current.process_owner_id), (new_token, new_owner))
        self.assertTrue(lock_ownership.is_locked(key))
        self.retire(new_owner)
        self.assertFalse(lock_ownership.is_locked(key))

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull', LOCK_TIMEOUT=13))
    def test_job_owned_token_keeps_full_configured_lease_not_job_timeout(self):
        row = Widget.objects.create(status='draft')
        BlockedOwnerProcess(instance=row, field_name='status').finish(
            nested_pk=row.pk, marker_path='unused', stop_path='unused')
        job = TransitionMessage.objects.get()
        self.assertEqual(job.timeout_seconds, 2)
        owner_id = lock_ownership.create_process_owner(job.pk)
        state = State(row, 'status')
        with patch.object(lock_ownership, '_process_owner', (os.getpid(), owner_id, job.pk)):
            self.assertTrue(state.lock())
        saved = StateLock.objects.get(key=state._get_hash())
        self.assertGreater(saved.expires_at, timezone.now() + timedelta(seconds=12))
        self.assertLess(saved.expires_at, timezone.now() + timedelta(seconds=14))
        self.assertEqual(saved.process_owner_id, owner_id)
        owner = JobProcessOwner.objects.get(pk=owner_id)
        self.assertEqual(owner.job_id, job.pk)
        state.unlock()
        self.retire(owner_id)

    def test_expired_job_token_replacement_survives_old_owner_cleanup(self):
        old_owner = lock_ownership.create_process_owner()
        new_owner = lock_ownership.create_process_owner()
        key = 'expired-job-token'
        with patch.object(lock_ownership, '_process_owner', (os.getpid(), old_owner, None)):
            old_token = lock_ownership.acquire_lock(key, 7200)
        StateLock.objects.filter(key=key).update(expires_at=timezone.now() - timedelta(seconds=1))
        with patch.object(lock_ownership, '_process_owner', (os.getpid(), new_owner, None)):
            new_token = lock_ownership.acquire_lock(key, 7200)
        self.assertIsNotNone(new_token)
        lock_ownership.release_lock(key, old_token)
        self.retire(old_owner)
        current = StateLock.objects.get(key=key)
        self.assertEqual((current.token, current.process_owner_id), (new_token, new_owner))
        self.retire(new_owner)

    def test_expired_ordinary_lock_replacement_survives_old_token_release(self):
        key = 'ordinary-replacement'
        old_token = lock_ownership.acquire_lock(key, 7200)
        StateLock.objects.filter(key=key).update(expires_at=timezone.now() - timedelta(seconds=1))
        new_token = lock_ownership.acquire_lock(key, 7200)
        self.assertIsNotNone(new_token)
        self.assertNotEqual(old_token, new_token)
        lock_ownership.release_lock(key, old_token)
        self.assertEqual(StateLock.objects.get(key=key).token, new_token)
        self.assertTrue(lock_ownership.is_locked(key))
        lock_ownership.release_lock(key, new_token)


def stop_after_nested_lock(instance, *, marker_path, stop_path, **kwargs):
    marker = Path(marker_path)
    if marker.exists():
        return
    marker.write_text('The nested operation has its lock.\n')
    until = time.monotonic() + 5
    while not Path(stop_path).exists():
        if time.monotonic() >= until:
            raise TimeoutError('The test did not ask the first job process to stop.')
        time.sleep(0.01)
    os.kill(os.getpid(), signal.SIGKILL)


class BlockedNestedProcess(Process):
    process_name = 'blocked_nested'
    transitions = [Transition('finish', sources=['draft'], target='finished',
                              side_effects=[stop_after_nested_lock])]


def finish_blocked_nested(instance, *, nested_pk, marker_path, stop_path, **kwargs):
    nested = Widget.objects.get(pk=nested_pk)
    BlockedNestedProcess(instance=nested, field_name='status').finish(
        marker_path=marker_path, stop_path=stop_path)


class BlockedOwnerProcess(Process):
    process_name = 'blocked_owner'
    transitions = [BackgroundTransition('finish', sources=['draft'], target='finished',
                                        queue='owner_cleanup_review', timeout=2,
                                        side_effects=[finish_blocked_nested])]


def save_database_work_before_stop(instance, *, marker_path, nested_pk=None, **kwargs):
    instance.se_log += 'saved,'
    instance.save(update_fields=['se_log'])
    if nested_pk is not None:
        nested = Widget.objects.get(pk=nested_pk)
        DatabaseOnlyProcess(instance=nested, field_name='status').finish(marker_path=marker_path)
        return
    marker = Path(marker_path)
    if not marker.exists():
        marker.write_text(json.dumps({'job_pid': os.getpid(), 'worker_group': os.getpgrp()}))
        time.sleep(30)
        raise RuntimeError('The test did not stop the first worker.')


class DatabaseOnlyProcess(Process):
    process_name = 'owner_database_only'
    transitions = [DatabaseTransition('finish', sources=['draft'], target='finished',
                                      side_effects=[save_database_work_before_stop])]


def finish_database_work(instance, *, nested_pk, marker_path, **kwargs):
    DatabaseOnlyProcess(instance=instance, field_name='status').finish(
        nested_pk=nested_pk, marker_path=marker_path)


class DatabaseOnlyJobProcess(Process):
    process_name = 'owner_database_only_job'
    transitions = [BackgroundTransition('finish', sources=['draft', 'finished'], atomic=False,
                                        queue='owner_database_review', timeout=40,
                                        side_effects=[finish_database_work])]


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull',
                                            TRANSITION_MESSAGE_RETRY_MINUTES=0))
class WorkerOwnerRecoveryTests(TransactionTestCase):
    def wait_for_file(self, path):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if Path(path).exists():
                return
            time.sleep(0.01)
        self.fail('The first job process did not enter the nested operation.')

    def separate_worker_results(self):
        read_fd, write_fd = os.pipe()
        connections.close_all()
        worker_pid = os.fork()
        if worker_pid == 0:
            os.close(read_fd)
            try:
                results = [pull.run_once(['owner_cleanup_review'], isolate=True) for _ in range(3)]
                os.write(write_fd, json.dumps(results).encode())
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(write_fd)
        until = time.monotonic() + 5
        try:
            while time.monotonic() < until:
                ended, status = os.waitpid(worker_pid, os.WNOHANG)
                if ended:
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                    return json.loads(os.read(read_fd, 1024))
                time.sleep(0.01)
            os.kill(worker_pid, signal.SIGKILL)
            os.waitpid(worker_pid, 0)
            self.fail('The second worker did not return within its test limit.')
        finally:
            os.close(read_fd)

    def unstarted_job(self):
        parent = Widget.objects.create(status='draft')
        nested = Widget.objects.create(status='draft')
        BlockedOwnerProcess(instance=parent, field_name='status').finish(
            nested_pk=nested.pk, marker_path='unused-startup-marker', stop_path='unused-startup-stop')
        return TransitionMessage.objects.get()

    def finish_pending_cleanup(self, attempts, pending):
        until = time.monotonic() + 5
        while (attempts or pending) and time.monotonic() < until:
            pull._harvest(attempts, pending, block=True, max_wait=0.2)
        self.assertFalse(attempts)
        self.assertFalse(pending)

    def test_lost_registration_commit_reply_removes_the_committed_reservation(self):
        job = self.unstarted_job()
        attempts, pending = {}, deque()
        real_commit = connection._commit
        committed_owners = []
        injected = False

        def commit_then_lose_reply():
            nonlocal injected
            real_commit()
            if not injected:
                injected = True
                committed_owners.extend(JobProcessOwner.objects.filter(job_id=job.pk).values_list('id', flat=True))
                raise OperationalError('The commit reply was lost after PostgreSQL committed.')

        with patch.object(connection, '_commit', side_effect=commit_then_lose_reply), \
                patch.object(pull.os, 'fork') as fork:
            with self.assertRaisesRegex(OperationalError, 'commit reply was lost'):
                pull._start_attempt(job.pk, attempts, pending=pending)
            fork.assert_not_called()
        self.assertEqual(len(committed_owners), 1, 'The reservation really committed before the error.')
        self.assertFalse(JobProcessOwner.objects.filter(pk=committed_owners[0]).exists())
        self.assertFalse(attempts)
        self.assertFalse(pending)
        job.refresh_from_db()
        self.assertEqual(job.errors_count, 0)
        self.assertIsNone(job.started_at)
        self.assertEqual(pull.claim_next(['owner_cleanup_review']), job.pk)

    def test_failed_fork_retains_exact_cleanup_until_the_owner_row_is_available(self):
        job = self.unstarted_job()
        attempts, pending = {}, deque()
        blocker, owner_ids = None, []

        def fail_fork_with_blocked_cleanup():
            nonlocal blocker
            owner_ids.append(JobProcessOwner.objects.get(job_id=job.pk).pk)
            blocker = connection.copy(alias='independent_failed_start_blocker')
            blocker.ensure_connection()
            blocker.set_autocommit(False)
            with blocker.cursor() as cursor:
                cursor.execute(
                    f'SELECT id FROM {connection.ops.quote_name(JobProcessOwner._meta.db_table)} '
                    'WHERE id=%s FOR UPDATE', [owner_ids[0]])
            raise OSError('The operating system refused to start the job process.')

        try:
            with patch.object(pull.os, 'fork', side_effect=fail_fork_with_blocked_cleanup):
                with self.assertRaisesRegex(OSError, 'refused to start'):
                    pull._start_attempt(job.pk, attempts, pending=pending)
            self.assertFalse(attempts)
            self.assertEqual(len(pending), 1)
            self.assertTrue(pending[0].cleanup_only)
            self.assertEqual(pending[0].owner_id, owner_ids[0])
            job.refresh_from_db()
            self.assertEqual(job.errors_count, 0)
            self.assertIsNone(job.started_at)
            self.assertEqual(pull.claim_next(['owner_cleanup_review']), job.pk)
        finally:
            if blocker is not None:
                blocker.rollback()
                blocker.close()
            self.finish_pending_cleanup(attempts, pending)
        self.assertFalse(JobProcessOwner.objects.filter(pk=owner_ids[0]).exists())
        job.refresh_from_db()
        self.assertEqual(job.errors_count, 0)
        self.assertFalse(job.ended_in_failure)
        self.assertEqual(pull.claim_next(['owner_cleanup_review']), job.pk)

    def test_unstarted_cleanup_waits_for_a_registration_that_is_not_yet_visible(self):
        job = self.unstarted_job()
        owner_id = uuid4()
        inserted, allow_commit = Event(), Event()

        def delayed_registration():
            with transaction.atomic():
                saved = lock_ownership.create_process_owner(job.pk, owner_id=owner_id)
                inserted.set()
                if not allow_commit.wait(5):
                    raise RuntimeError('The test did not finish the registration transaction.')
                return saved

        cleanup = pull._Attempt(pk=job.pk, timeout_seconds=None, deadline=None,
                                owner_id=owner_id, cleanup_only=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            registering = pool.submit(independent_call, delayed_registration)
            try:
                self.assertTrue(inserted.wait(3))
                self.assertFalse(JobProcessOwner.objects.filter(pk=owner_id).exists())
                self.assertFalse(pull._try_account(cleanup),
                                 'Cleanup must wait for the in-flight registration, not report it absent.')
                self.assertEqual(cleanup.owner_id, owner_id)
            finally:
                allow_commit.set()
            self.assertEqual(registering.result(timeout=5), owner_id)
        self.assertTrue(pull._try_account(cleanup))
        self.assertFalse(JobProcessOwner.objects.filter(pk=owner_id).exists())
        job.refresh_from_db()
        self.assertEqual(job.errors_count, 0)
        self.assertEqual(pull.claim_next(['owner_cleanup_review']), job.pk)

    def test_preselected_worker_does_not_spend_retry_budget_on_another_owners_lock(self):
        job = self.unstarted_job()
        selected = pull.claim_next(['owner_cleanup_review'])
        self.assertEqual(selected, job.pk)
        old_owner = lock_ownership.create_process_owner(job.pk)
        nested = Widget.objects.get(pk=job.kwargs['nested_pk'])
        state = State(nested, 'status')
        with patch.object(lock_ownership, '_process_owner', (os.getpid(), old_owner, job.pk)):
            self.assertTrue(state.lock())
        original_token = StateLock.objects.get(key=state._get_hash()).token
        attempts, pending = {}, deque()
        try:
            pull._start_attempt(selected, attempts, pending=pending)
            self.assertTrue(attempts)
            self.finish_pending_cleanup(attempts, pending)
            job.refresh_from_db()
            nested.refresh_from_db()
            self.assertEqual(job.errors_count, 0)
            self.assertFalse(job.is_completed)
            self.assertFalse(job.ended_in_failure)
            self.assertEqual((nested.status, nested.se_log), ('draft', ''))
            self.assertEqual(StateLock.objects.get(key=state._get_hash()).token, original_token)
            self.assertTrue(JobProcessOwner.objects.filter(pk=old_owner).exists())
        finally:
            state.unlock()
            with transaction.atomic():
                lock_ownership.retire_process_owner(old_owner)
        self.assertEqual(pull.claim_next(['owner_cleanup_review']), job.pk)

    def test_database_only_work_recovers_after_the_entire_worker_stops(self):
        parent = Widget.objects.create(status='draft')
        nested = Widget.objects.create(status='draft')
        worker_pid = None
        with tempfile.TemporaryDirectory(prefix='dl-database-worker-review-') as directory:
            marker = str(Path(directory) / 'entered')
            DatabaseOnlyJobProcess(instance=parent, field_name='status').finish(
                nested_pk=nested.pk, marker_path=marker)
            job = TransitionMessage.objects.get()
            connections.close_all()
            worker_pid = os.fork()
            if worker_pid == 0:
                os.setsid()
                status = 0 if pull.run_once(['owner_database_review'], isolate=True) else 1
                os._exit(status)
            try:
                self.wait_for_file(marker)
                identity = json.loads(Path(marker).read_text())
                self.assertEqual(identity['worker_group'], worker_pid)
                self.assertNotEqual(identity['job_pid'], worker_pid)
                self.assertFalse(StateLock.objects.exists())
                original_owner = JobProcessOwner.objects.get(job_id=job.pk).pk
                os.killpg(worker_pid, signal.SIGKILL)
                _, status = os.waitpid(worker_pid, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), -signal.SIGKILL)
                worker_pid = None
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    if pull.claim_next(['owner_database_review']) == job.pk:
                        break
                    time.sleep(0.01)
                else:
                    self.fail('An owner without any state lock delayed replacement-worker recovery.')
                for row in (parent, nested):
                    row.refresh_from_db()
                    self.assertEqual((row.status, row.se_log), ('draft', ''))
                self.assertTrue(JobProcessOwner.objects.filter(pk=original_owner).exists())
                self.assertTrue(pull.run_once(['owner_database_review'], isolate=True))
                job.refresh_from_db()
                self.assertTrue(job.is_completed)
                self.assertEqual(job.errors_count, 0)
                for row in (parent, nested):
                    row.refresh_from_db()
                    self.assertEqual((row.status, row.se_log), ('finished', 'saved,'))
                self.assertFalse(StateLock.objects.exists())
            finally:
                if worker_pid is not None:
                    os.killpg(worker_pid, signal.SIGKILL)
                    os.waitpid(worker_pid, 0)

    def test_another_worker_preserves_retry_budget_while_stopped_owner_cleanup_waits(self):
        parent = Widget.objects.create(status='draft')
        nested = Widget.objects.create(status='draft')
        attempts, pending = {}, deque()
        blocker = None
        with tempfile.TemporaryDirectory(prefix='dl-owner-review-') as directory:
            marker, stop = str(Path(directory) / 'entered'), str(Path(directory) / 'stop')
            BlockedOwnerProcess(instance=parent, field_name='status').finish(
                nested_pk=nested.pk, marker_path=marker, stop_path=stop)
            job = TransitionMessage.objects.get()
            pull._start_attempt(job.pk, attempts, pending=pending)
            self.assertTrue(attempts)
            try:
                self.wait_for_file(marker)
                attempt = next(iter(attempts.values()))
                blocker = connection.copy(alias='independent_owner_blocker')
                blocker.ensure_connection()
                blocker.set_autocommit(False)
                with blocker.cursor() as cursor:
                    cursor.execute(
                        f'SELECT id FROM {connection.ops.quote_name(JobProcessOwner._meta.db_table)} '
                        'WHERE id=%s FOR UPDATE', [attempt.owner_id])
                Path(stop).touch()
                pull._harvest(attempts, pending, block=True, max_wait=2)
                self.assertFalse(attempts)
                self.assertEqual(len(pending), 1)
                self.assertTrue(State(nested, 'status').is_locked())
                job.refresh_from_db()
                self.assertEqual(job.errors_count, 0)
                self.assertEqual(self.separate_worker_results(), [False, False, False])
                job.refresh_from_db()
                self.assertEqual(job.errors_count, 0)
                self.assertFalse(job.ended_in_failure)
            finally:
                if blocker is not None:
                    blocker.rollback()
                    blocker.close()
                Path(stop).touch()
                until = time.monotonic() + 5
                while (attempts or pending) and time.monotonic() < until:
                    pull._harvest(attempts, pending, block=True, max_wait=0.2)
                for pid in attempts:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            self.assertFalse(attempts)
            self.assertFalse(pending)
            job.refresh_from_db()
            self.assertEqual(job.errors_count, 1)
            self.assertFalse(State(nested, 'status').is_locked())
            self.assertTrue(pull.run_once(['owner_cleanup_review'], isolate=True))
            job.refresh_from_db()
            parent.refresh_from_db()
            nested.refresh_from_db()
            self.assertEqual((parent.status, nested.status), ('finished', 'finished'))
            self.assertTrue(job.is_completed)
            self.assertEqual(job.errors_count, 1)

    def test_blocked_owner_table_cannot_delay_another_job_deadline(self):
        parent = Widget.objects.create(status='draft')
        nested = Widget.objects.create(status='draft')
        attempts, pending = {}, deque()
        blocker = None
        with tempfile.TemporaryDirectory(prefix='dl-owner-table-review-') as directory:
            marker, stop = str(Path(directory) / 'entered'), str(Path(directory) / 'stop')
            BlockedOwnerProcess(instance=parent, field_name='status').finish(
                nested_pk=nested.pk, marker_path=marker, stop_path=stop)
            job = TransitionMessage.objects.get()
            pull._start_attempt(job.pk, attempts, pending=pending)
            self.assertTrue(attempts)
            try:
                self.wait_for_file(marker)
                blocker = connection.copy(alias='independent_owner_table_blocker')
                blocker.ensure_connection()
                blocker.set_autocommit(False)
                with blocker.cursor() as cursor:
                    cursor.execute(
                        f'LOCK TABLE {connection.ops.quote_name(JobProcessOwner._meta.db_table)} '
                        'IN ACCESS EXCLUSIVE MODE')
                started = time.monotonic()
                with self.assertRaises(OperationalError):
                    pull._start_attempt(None, attempts, pending=pending)
                self.assertLess(time.monotonic() - started, 0.8)
                attempt = next(iter(attempts.values()))
                pull._harvest(attempts, pending, block=True, max_wait=3)
                self.assertFalse(attempts)
                self.assertEqual(len(pending), 2)
                stopped_jobs = [item for item in pending if not item.cleanup_only]
                failed_starts = [item for item in pending if item.cleanup_only]
                self.assertEqual(len(stopped_jobs), 1)
                self.assertEqual(len(failed_starts), 1)
                self.assertTrue(stopped_jobs[0].killed)
                self.assertEqual(stopped_jobs[0].exit_code, -signal.SIGKILL)
                self.assertIsNone(failed_starts[0].pk)
            finally:
                if blocker is not None:
                    blocker.rollback()
                    blocker.close()
                Path(stop).touch()
                until = time.monotonic() + 5
                while (attempts or pending) and time.monotonic() < until:
                    pull._harvest(attempts, pending, block=True, max_wait=0.2)
                for pid in attempts:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            self.assertFalse(attempts)
            self.assertFalse(pending)
            job.refresh_from_db()
            self.assertEqual(job.errors_count, 1)
            self.assertIn('[timeout]', job.last_error_message)
            self.assertFalse(State(nested, 'status').is_locked())
