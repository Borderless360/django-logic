"""Database table locks must not prevent the worker from stopping overdue jobs."""
import os
import multiprocessing
import signal
import threading
import time
import unittest
from collections import deque
from contextlib import contextmanager
from unittest.mock import patch

from django.db import OperationalError, connection, connections, transaction
from django.test import TransactionTestCase

from django_logic.background import pull, runner
from django_logic.background.models import TransitionMessage
from django_logic.testing import open_transition_message
from tests.background.models import Widget
from tests.stability.base import requires_postgres


@requires_postgres
@unittest.skipUnless(hasattr(os, 'fork'), 'needs os.fork')
class SupervisorDatabaseWaitTests(TransactionTestCase):
    @contextmanager
    def _table_lock(self):
        ready = threading.Event()
        acquired = threading.Event()
        release = threading.Event()
        backend = []
        errors = []
        table = connection.ops.quote_name(TransitionMessage._meta.db_table)

        def hold_table():
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '4s'")
                        cursor.execute('SELECT pg_backend_pid()')
                        backend.append(cursor.fetchone()[0])
                        ready.set()
                        cursor.execute(f'LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE')
                    acquired.set()
                    release.wait(timeout=4)
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        holder = threading.Thread(target=hold_table)
        holder.start()
        try:
            self.assertTrue(ready.wait(timeout=2), errors)
            deadline = time.monotonic() + 2
            with connection.cursor() as cursor:
                while True:
                    cursor.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks "
                        "WHERE pid = %s AND mode = 'AccessExclusiveLock')", backend,
                    )
                    if cursor.fetchone()[0]:
                        break
                    self.assertLess(time.monotonic(), deadline, errors)
                    time.sleep(0.01)
            yield acquired
        finally:
            release.set()
            holder.join(timeout=5)
            self.assertFalse(holder.is_alive(), 'the table lock holder did not stop')
            self.assertEqual(errors, [], 'the table lock holder failed')

    @staticmethod
    def _stop_job(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def test_queued_table_lock_does_not_prevent_a_real_job_timeout(self):
        running = open_transition_message(
            Widget.objects.create(status='fulfilling'), 'process', 'hang',
        )
        TransitionMessage.objects.filter(pk=running.pk).update(timeout_seconds=1)
        failed = open_transition_message(
            Widget.objects.create(status='fulfilling'), 'process', 'fulfil',
        )
        attempts = {}
        pending = deque([
            pull._Attempt(pk=failed.pk, timeout_seconds=None, deadline=None, exit_code=3),
        ])
        job_started = multiprocessing.get_context('fork').Event()
        execute_attempt = runner._execute_attempt

        def execute_when_locked(*args):
            job_started.set()
            return execute_attempt(*args)

        with patch.object(runner, '_execute_attempt', execute_when_locked):
            pull._start_attempt(running.pk, attempts)
        pid = next(iter(attempts))
        try:
            self.assertTrue(job_started.wait(timeout=2), 'the job did not start')
            started = time.monotonic()
            with self._table_lock() as acquired:
                self.assertFalse(acquired.is_set())
                while attempts and time.monotonic() - started < 2:
                    pull._harvest(attempts, pending, block=False)
                    time.sleep(0.01)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(attempts, {})
                # The stopped job releases its table lock. Failure writes wait
                # separately until the schema operation releases its lock.
                self.assertTrue(acquired.wait(timeout=0.5))
                self.assertEqual({attempt.pk for attempt in pending}, {failed.pk, running.pk})
            while pending:
                pull._harvest(attempts, pending, block=True)
            for row, expected in ((failed, '[crashed]'), (running, '[timeout]')):
                row.refresh_from_db()
                self.assertEqual(row.errors_count, 1)
                self.assertIn(expected, row.last_error_message)
                self.assertFalse(row.is_completed)
        finally:
            if pid in attempts:
                self._stop_job(pid)

    def test_blocked_timeout_lookup_starts_no_job_and_can_be_retried(self):
        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(widget, 'process', 'fulfil')
        attempts = {}
        with self._table_lock() as acquired, patch.object(pull.os, 'fork') as fork:
            self.assertTrue(acquired.wait(timeout=0.5))
            started = time.monotonic()
            with self.assertRaises(OperationalError):
                pull._start_attempt(row.pk, attempts)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(attempts, {})
            fork.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.errors_count, 0)
        self.assertFalse(row.is_completed)
        pull._start_attempt(row.pk, attempts)
        pid = next(iter(attempts))
        try:
            pull._harvest(attempts, deque(), block=True, max_wait=3)
            self.assertEqual(attempts, {})
            row.refresh_from_db()
            widget.refresh_from_db()
            self.assertTrue(row.is_completed)
            self.assertEqual(row.errors_count, 0)
            self.assertEqual(widget.status, 'fulfilled')
            self.assertEqual(widget.se_log, 'ok,')
        finally:
            if pid in attempts:
                self._stop_job(pid)

    def test_failure_recording_preserves_connection_timeouts(self):
        row = open_transition_message(
            Widget.objects.create(status='fulfilling'), 'process', 'fulfil',
        )
        with connection.cursor() as cursor:
            cursor.execute('SHOW statement_timeout')
            statement_timeout = cursor.fetchone()[0]
            cursor.execute('SHOW lock_timeout')
            lock_timeout = cursor.fetchone()[0]
            try:
                for statement, lock in (('0', '0'), ('500ms', '40ms')):
                    with self.subTest(statement=statement, lock=lock):
                        cursor.execute("SELECT set_config('statement_timeout', %s, false)", [statement])
                        cursor.execute("SELECT set_config('lock_timeout', %s, false)", [lock])
                        pull._record_attempt_error(row.pk, 'original failure')
                        with self._table_lock():
                            with self.assertRaises(OperationalError):
                                pull._record_attempt_error(row.pk, 'unrecorded failure')
                        cursor.execute('SHOW statement_timeout')
                        self.assertEqual(cursor.fetchone()[0], statement)
                        cursor.execute('SHOW lock_timeout')
                        self.assertEqual(cursor.fetchone()[0], lock)
            finally:
                cursor.execute("SELECT set_config('statement_timeout', %s, false)", [statement_timeout])
                cursor.execute("SELECT set_config('lock_timeout', %s, false)", [lock_timeout])
        row.refresh_from_db()
        self.assertEqual(row.errors_count, 2)
        self.assertEqual(row.last_error_message, 'original failure')
