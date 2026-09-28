"""Application report delivery cannot change a job's recorded outcome."""
from collections import deque
import ctypes
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from django_logic import conf
from django_logic.background import pull, reporting
from django_logic.background.models import TransitionMessage
from django_logic.logger import logger
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres


_handler = None
_directory = None


class BufferedReports(logging.Handler):
    def __init__(self, destination):
        super().__init__()
        self.destination = destination
        self.pid = os.getpid()
        self.records = []
        self.buffer_lock = threading.Lock()

    def _local_state(self):
        if self.pid != os.getpid():
            self.pid = os.getpid()
            self.records = []
            self.buffer_lock = threading.Lock()

    def emit(self, record):
        self._local_state()
        with self.buffer_lock:
            self.records.append({'pid': os.getpid(), 'text': record.getMessage()})

    def send(self):
        self._local_state()
        with self.buffer_lock:
            with self.destination.open('a') as stream:
                for record in self.records:
                    stream.write(json.dumps(record) + '\n')
            self.records = []


def finish_reports():
    _handler.send()


def block_finish():
    (Path(_directory) / 'finish_started').write_text(str(os.getpid()))
    time.sleep(30)


def raise_finish():
    raise SystemExit(19)


def exit_finish():
    os._exit(23)


def slow_finish():
    time.sleep(0.15)
    finish_reports()


def hold_python_finish():
    (Path(_directory) / 'finish_started').write_text(str(os.getpid()))
    ctypes.PyDLL(None).sleep(30)


def _report(*args):
    logger.warning('job report')


def _raise_after_report(*args):
    _report()
    raise RuntimeError('job failed')


def _hang(*args):
    time.sleep(30)


def _finish_settings(name='finish_reports'):
    return dl_settings(JOB_PROCESS_FINISH=f'{__name__}.{name}')


class ReportingSettingsTests(SimpleTestCase):
    def test_default_has_no_hook(self):
        with override_settings(DJANGO_LOGIC=dl_settings()):
            self.assertIsNone(conf.job_process_finish())

    def test_dotted_callable_is_resolved(self):
        with override_settings(DJANGO_LOGIC=_finish_settings()):
            self.assertIs(conf.job_process_finish(), finish_reports)

    def test_invalid_setting_names_the_setting(self):
        for value in ('', 7, finish_reports, 'no_such_reporting_module.finish',
                      f'{__name__}._directory'):
            with self.subTest(value=value), override_settings(
                DJANGO_LOGIC=dl_settings(JOB_PROCESS_FINISH=value),
            ):
                with self.assertRaisesMessage(ImproperlyConfigured, 'JOB_PROCESS_FINISH'):
                    conf.job_process_finish()


@unittest.skipUnless(hasattr(os, 'fork'), 'requires os.fork')
class ProcessReportingTests(TransactionTestCase):
    def setUp(self):
        global _handler, _directory
        temporary = tempfile.TemporaryDirectory(prefix='dl_job_reports_')
        self.addCleanup(temporary.cleanup)
        _directory = temporary.name
        self.destination = Path(temporary.name) / 'reports.jsonl'
        _handler = BufferedReports(self.destination)
        logger.addHandler(_handler)
        self.addCleanup(logger.removeHandler, _handler)
        self.attempts = {}
        self.pending = deque()
        self.addCleanup(self._stop_remaining)
        self.record = self.enterContext(patch.object(pull, '_record_attempt_error'))
        self.enterContext(patch.object(reporting, 'FINISH_SECONDS', 0.35))

    def _stop_remaining(self):
        for pid, attempt in self.attempts.items():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            if attempt.result_fd is not None:
                os.close(attempt.result_fd)

    def _start(self, pk=None, body=_report, timeout=None):
        with patch.object(pull, '_run_safety_nets', side_effect=body), \
                patch('django_logic.background.runner.run_background_transition', side_effect=body), \
                patch.object(TransitionMessage.objects, 'filter') as rows:
            rows.return_value.values_list.return_value.first.return_value = timeout
            pull._start_attempt(pk, self.attempts)
        pid = next(reversed(self.attempts))
        return pid, self.attempts[pid]

    def _wait(self):
        deadline = time.monotonic() + 5
        while self.attempts or self.pending:
            self.assertLess(time.monotonic(), deadline, 'The job process did not stop.')
            pull._harvest(self.attempts, self.pending, block=True, max_wait=0.05)

    def _reports(self):
        if not self.destination.exists():
            return []
        return [json.loads(line) for line in self.destination.read_text().splitlines()]

    @override_settings(DJANGO_LOGIC=_finish_settings())
    def test_job_and_safety_pass_send_only_their_own_reports(self):
        logger.warning('worker report')
        worker_pid = os.getpid()
        # The application replaces its private lock before acquiring it after fork.
        with _handler.buffer_lock:
            job_pid, job = self._start(pk=1)
            safety_pid, safety = self._start()
            descriptors = [job.result_fd, safety.result_fd]
            self._wait()
        _handler.send()
        self.assertCountEqual(self._reports(), [
            {'pid': worker_pid, 'text': 'worker report'},
            {'pid': job_pid, 'text': 'job report'},
            {'pid': safety_pid, 'text': 'job report'},
        ])
        self.assertEqual(job.exit_code, 0)
        self.assertEqual(safety.exit_code, 0)
        self.record.assert_not_called()
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    @override_settings(DJANGO_LOGIC=dl_settings())
    def test_no_hook_keeps_the_existing_exit_without_a_pipe(self):
        with patch.object(pull.os, 'pipe', side_effect=AssertionError('unexpected pipe')):
            _, attempt = self._start()
            self._wait()
        self.assertIsNone(attempt.result_fd)
        self.assertEqual(self._reports(), [])

    @override_settings(DJANGO_LOGIC=_finish_settings('block_finish'))
    def test_blocked_finish_is_bounded_without_a_job_timeout(self):
        started = time.monotonic()
        pid, attempt = self._start(pk=1)
        self._wait()
        self.assertEqual((Path(_directory) / 'finish_started').read_text(), str(pid))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(attempt.exit_code, 0)
        self.record.assert_not_called()

    @override_settings(DJANGO_LOGIC=_finish_settings('hold_python_finish'))
    def test_worker_stops_delivery_when_the_reporting_thread_holds_python(self):
        pid, attempt = self._start(pk=1)
        stopped = []

        def stop_if_stuck():
            stopped.append(True)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        watchdog = threading.Timer(3, stop_if_stuck)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        started = time.monotonic()
        pull._harvest(self.attempts, self.pending, block=True)
        watchdog.cancel()
        self.assertEqual(stopped, [])
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((Path(_directory) / 'finish_started').read_text(), str(pid))
        self.assertEqual(attempt.exit_code, 0)
        self.record.assert_not_called()

    def test_finish_errors_or_process_exit_preserve_the_job_result(self):
        for name in ('raise_finish', 'exit_finish'):
            with self.subTest(name=name), override_settings(DJANGO_LOGIC=_finish_settings(name)):
                _, attempt = self._start(pk=1)
                self._wait()
                self.assertEqual(attempt.exit_code, 0)
        self.record.assert_not_called()

    @override_settings(DJANGO_LOGIC=_finish_settings('slow_finish'))
    def test_report_delivery_has_its_own_limit_after_a_short_job(self):
        _, attempt = self._start(pk=1, timeout=0.08)
        self._wait()
        self.assertEqual(attempt.exit_code, 0)
        self.assertEqual(len(self._reports()), 1)
        self.record.assert_not_called()

    @override_settings(DJANGO_LOGIC=_finish_settings('block_finish'))
    def test_blocked_finish_does_not_delay_another_jobs_timeout(self):
        self._start(pk=1)
        _, timed = self._start(pk=2, body=_hang, timeout=0.12)
        started = time.monotonic()
        while not self.record.called:
            self.assertLess(time.monotonic() - started, 0.3)
            pull._harvest(self.attempts, self.pending, block=True, max_wait=0.02)
        self.assertEqual(self.record.call_args.args[0], 2)
        self.assertIn('[timeout]', self.record.call_args.args[1])
        self.assertTrue(timed.killed)
        self._wait()
        self.record.assert_called_once()

    @override_settings(DJANGO_LOGIC=_finish_settings())
    def test_engine_exception_keeps_failure_status_and_sends_reports(self):
        _, attempt = self._start(pk=1, body=_raise_after_report)
        self._wait()
        self.assertEqual(attempt.exit_code, 1)
        self.record.assert_called_once()
        self.assertIn('[crashed]', self.record.call_args.args[1])
        self.assertTrue(any(row['text'] == 'job report' for row in self._reports()))

    @override_settings(DJANGO_LOGIC=_finish_settings())
    def test_hard_job_exit_does_not_claim_report_delivery(self):
        _, attempt = self._start(pk=1, body=lambda *args: os._exit(17))
        self._wait()
        self.assertIsNone(attempt.work_exit_code)
        self.assertEqual(attempt.exit_code, 17)
        self.assertEqual(self._reports(), [])
        self.record.assert_called_once()

    @override_settings(DJANGO_LOGIC=_finish_settings())
    def test_fork_failure_closes_both_pipe_descriptors(self):
        descriptors = os.pipe()
        with patch.object(pull.os, 'pipe', return_value=descriptors), \
                patch.object(pull.os, 'fork', side_effect=OSError('cannot fork')):
            with self.assertRaisesMessage(OSError, 'cannot fork'):
                self._start()
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(
    BACKGROUND_EXECUTION='pull',
    JOB_PROCESS_FINISH=f'{__name__}.block_finish',
))
class RecordedFailureReportingTests(TransactionTestCase):
    def test_delivery_timeout_does_not_record_a_retry_failure_twice(self):
        global _directory
        with tempfile.TemporaryDirectory(prefix='dl_recorded_failure_') as directory:
            _directory = directory
            widget = Widget.objects.create(status='draft')
            widget.process.crash()
            row = TransitionMessage.objects.get(instance_id=str(widget.pk))
            TransitionMessage.objects.filter(pk=row.pk).update(timeout_seconds=1)
            attempts = {}
            pending = deque()
            with patch.object(reporting, 'FINISH_SECONDS', 1.25):
                pull._start_attempt(row.pk, attempts)
                deadline = time.monotonic() + 5
                try:
                    while attempts or pending:
                        self.assertLess(time.monotonic(), deadline)
                        pull._harvest(attempts, pending, block=True, max_wait=0.1)
                finally:
                    for pid, attempt in attempts.items():
                        try:
                            os.kill(pid, signal.SIGKILL)
                            os.waitpid(pid, 0)
                        except (ProcessLookupError, ChildProcessError):
                            pass
                        if attempt.result_fd is not None:
                            os.close(attempt.result_fd)
            row.refresh_from_db()
            self.assertEqual(row.errors_count, 1)
            self.assertIn('boom', row.last_error_message)
            self.assertFalse(row.is_completed)
