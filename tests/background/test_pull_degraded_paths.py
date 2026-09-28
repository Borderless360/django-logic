"""Worker failures do not suppress maintenance or affect an unrelated job."""
from collections import deque
import os
import select
import signal
import time
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.db import OperationalError
from django.test import SimpleTestCase

from django_logic.background import pull, safety_nets


class WorkerFailureIsolationTests(SimpleTestCase):
    def test_failed_notification_returns_normally_and_warns(self):
        connection = Mock()
        connection.cursor.side_effect = OperationalError('The database connection is unavailable.')
        with patch.object(pull, 'connections', {'default': connection}), \
                self.assertLogs('django-logic', level='WARNING') as captured:
            self.assertIsNone(pull.notify_workers())
        connection.cursor.assert_called_once_with()
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelname, 'WARNING')
        self.assertIn('NOTIFY failed', captured.records[0].getMessage())
        self.assertIn('database connection is unavailable', captured.records[0].getMessage())

    def test_cleanup_still_runs_when_stuck_detection_fails(self):
        calls = []

        def detect_stuck_transitions():
            calls.append('detect')
            raise RuntimeError('The stuck-row scan cannot finish.')

        def cleanup_completed_transitions():
            calls.append('cleanup')

        with patch.object(safety_nets, 'detect_stuck_transitions', detect_stuck_transitions), \
                patch.object(safety_nets, 'cleanup_completed_transitions', cleanup_completed_transitions), \
                self.assertLogs('django-logic', level='ERROR') as captured:
            self.assertIsNone(pull._run_safety_nets())
        self.assertEqual(calls, ['detect', 'cleanup'])
        self.assertEqual(len(captured.records), 1)
        self.assertIn('detect_stuck_transitions failed', captured.records[0].getMessage())
        self.assertIn('stuck-row scan cannot finish', captured.records[0].getMessage())


class WorkerPollDelayTests(SimpleTestCase):
    def test_wait_uses_the_nearest_deadline_and_never_becomes_negative(self):
        attempts = {
            1: pull._Attempt(pk=1, timeout_seconds=30, deadline=120),
            2: pull._Attempt(pk=2, timeout_seconds=1, deadline=100.25),
        }
        with patch.object(pull.time, 'monotonic', return_value=100):
            self.assertEqual(pull._poll_delay(attempts, 5), 0.25)
            attempts[2].deadline = 100
            self.assertEqual(pull._poll_delay(attempts, 5), 0)
            attempts[2].deadline = 99
            self.assertEqual(pull._poll_delay(attempts, 5), 0)

    def test_jobs_without_a_near_deadline_leave_the_maximum_unchanged(self):
        with patch.object(pull.time, 'monotonic', return_value=100):
            for attempts in [
                {},
                {1: pull._Attempt(pk=1, timeout_seconds=None, deadline=None)},
                {1: pull._Attempt(pk=1, timeout_seconds=30, deadline=120)},
            ]:
                with self.subTest(attempts=attempts):
                    self.assertEqual(pull._poll_delay(attempts, 5), 5)

    def test_an_exit_already_collected_does_not_shorten_the_wait(self):
        attempts = {
            1: pull._Attempt(pk=1, timeout_seconds=1, deadline=99, reaped=True),
            2: pull._Attempt(pk=2, timeout_seconds=1, deadline=99, reaped=True, killed=True),
        }
        with patch.object(pull.time, 'monotonic', return_value=100):
            self.assertEqual(pull._poll_delay(attempts, 5), 5)

    def test_a_stopped_job_limits_the_wait_to_ten_milliseconds(self):
        attempts = {1: pull._Attempt(pk=1, timeout_seconds=1, deadline=99, killed=True)}
        with patch.object(pull.time, 'monotonic', return_value=100):
            self.assertEqual(pull._poll_delay(attempts, 5), 0.01)
            self.assertEqual(pull._poll_delay(attempts, 0.005), 0.005)


@skipUnless(hasattr(os, 'fork'), 'needs os.fork')
class UntrackedProcessTests(SimpleTestCase):
    def test_an_extra_process_exit_does_not_finish_or_fail_the_tracked_job(self):
        read_fd, write_fd = os.pipe()
        tracked_pid = os.fork()
        if tracked_pid == 0:
            os.close(write_fd)
            ready, _, _ = select.select([read_fd], [], [], 5)
            received = os.read(read_fd, 1) if ready else b''
            os._exit(0 if received == b'x' else 8)
        os.close(read_fd)
        extra_pid = os.fork()
        if extra_pid == 0:
            os.close(write_fd)
            os._exit(7)

        attempt = pull._Attempt(pk=42, timeout_seconds=None, deadline=None)
        attempts = {tracked_pid: attempt}
        pending = deque()
        collected = []
        real_waitpid = os.waitpid

        def collect_exit(*args):
            result = real_waitpid(*args)
            collected.append(result)
            return result

        try:
            with patch.object(pull.os, 'waitpid', collect_exit), \
                    patch.object(pull, '_record_attempt_error') as record_error:
                deadline = time.monotonic() + 2
                while not any(pid == extra_pid for pid, _ in collected):
                    self.assertLess(time.monotonic(), deadline, 'The extra process exit was not collected.')
                    pull._harvest(attempts, pending, block=False)
                    time.sleep(0.01)

                self.assertEqual(attempts, {tracked_pid: attempt})
                self.assertIsNone(attempt.exit_code)
                self.assertFalse(attempt.reaped)
                self.assertEqual(list(pending), [])
                record_error.assert_not_called()

                os.write(write_fd, b'x')
                pull._harvest(attempts, pending, block=True, max_wait=2)
                self.assertEqual(attempts, {})
                self.assertEqual(list(pending), [])
                self.assertEqual(attempt.exit_code, 0)
                record_error.assert_not_called()
        finally:
            os.close(write_fd)
            for pid in (tracked_pid, extra_pid):
                try:
                    ended, _ = real_waitpid(pid, os.WNOHANG)
                    if not ended:
                        os.kill(pid, signal.SIGKILL)
                        real_waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
