"""Safety-net callbacks cannot stop the worker from enforcing timeouts."""
import json
import os
import signal
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from django_logic import Process
from django_logic.background import BackgroundTransition
from django_logic.background import pull
from django_logic.background.models import TransitionMessage
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres


def _mark(directory, name):
    destination = Path(directory) / name
    temporary = destination.with_suffix(f'.{os.getpid()}')
    temporary.write_text(json.dumps({
        'pid': os.getpid(), 'time': time.monotonic(), 'wall_time': time.time(),
    }))
    temporary.replace(destination)


def _timed_effect(instance, marker_directory, **kwargs):
    _mark(marker_directory, 'attempt_started')
    time.sleep(2.5)
    _mark(marker_directory, 'attempt_late')
    time.sleep(10)


def _failure_callback(instance, marker_directory, callback_behavior, **kwargs):
    limit = time.monotonic() + 5
    while not (Path(marker_directory) / 'attempt_started').exists():
        if time.monotonic() >= limit:
            raise RuntimeError('The timed attempt did not start.')
        time.sleep(0.01)
    _mark(marker_directory, 'callback_started')
    if callback_behavior == 'exit':
        os._exit(17)
    time.sleep(10 if callback_behavior == 'hang' else 3)
    _mark(marker_directory, 'callback_finished')


class SafetyNetProcess(Process):
    process_name = 'safety_net_test'
    transitions = [
        BackgroundTransition(
            'timed', sources=['draft'], target='fulfilled',
            in_progress_state='fulfilling', failed_state='failed',
            queue='django_logic.safety_net_test', timeout=1,
            side_effects=[_timed_effect],
        ),
        BackgroundTransition(
            'exhausted', sources=['draft'], target='fulfilled',
            in_progress_state='fulfilling', failed_state='failed',
            queue='django_logic.safety_net_test',
            failure_callbacks=[_failure_callback],
        ),
    ]


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(
    BACKGROUND_EXECUTION='pull',
    TRANSITION_MESSAGE_MAX_ERRORS=3,
    TRANSITION_MESSAGE_RETRY_MINUTES=2,
))
class SafetyNetIsolationTests(TransactionTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='dl_safety_net_')
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def _enqueue(self, callback_behavior):
        timed = Widget.objects.create(status='draft')
        failed = Widget.objects.create(status='draft')
        SafetyNetProcess(instance=timed, field_name='status').timed(
            marker_directory=str(self.directory),
        )
        SafetyNetProcess(instance=failed, field_name='status').exhausted(
            marker_directory=str(self.directory), callback_behavior=callback_behavior,
        )
        exhausted = TransitionMessage.objects.get(instance_id=str(failed.pk))
        TransitionMessage.objects.filter(pk=exhausted.pk).update(errors_count=3)
        return timed, failed

    def _assert_outcomes(self, timed, failed):
        callback = json.loads((self.directory / 'callback_started').read_text())
        started = json.loads((self.directory / 'attempt_started').read_text())
        self.assertNotEqual(callback['pid'], os.getpid())
        self.assertNotEqual(callback['pid'], started['pid'])
        self.assertFalse((self.directory / 'attempt_late').exists())
        row = TransitionMessage.objects.get(instance_id=str(timed.pk))
        self.assertEqual(row.errors_count, 1)
        self.assertTrue(row.last_error_message.startswith('[timeout]'))
        self.assertLess(row.last_error_dt.timestamp() - started['wall_time'], 2)
        failed.refresh_from_db()
        self.assertEqual(failed.status, 'failed')
        self.assertTrue(TransitionMessage.objects.get(instance_id=str(failed.pk)).ended_in_failure)
        with self.assertRaises(ChildProcessError):
            os.waitpid(callback['pid'], os.WNOHANG)

    def test_slow_callback_does_not_delay_a_sibling_timeout(self):
        timed, failed = self._enqueue('slow')
        pull.run_worker(['django_logic.safety_net_test'], forever=False, concurrency=2)
        self._assert_outcomes(timed, failed)
        self.assertTrue((self.directory / 'callback_finished').exists())

    def test_callback_process_exit_does_not_stop_the_worker(self):
        timed, failed = self._enqueue('exit')
        with self.assertLogs('django-logic', level='WARNING') as logs:
            pull.run_worker(['django_logic.safety_net_test'], forever=False, concurrency=2)
        self._assert_outcomes(timed, failed)
        self.assertTrue(any('safety nets stopped with exit code 17' in line for line in logs.output))

    def test_hanging_safety_nets_are_stopped_and_reaped(self):
        timed, failed = self._enqueue('hang')
        with patch.object(pull, 'SAFETY_NET_TIMEOUT_SECONDS', 0.5):
            with self.assertLogs('django-logic', level='WARNING') as logs:
                pull.run_worker(['django_logic.safety_net_test'], forever=False, concurrency=2)
        self._assert_outcomes(timed, failed)
        self.assertFalse((self.directory / 'callback_finished').exists())
        self.assertTrue(any('safety nets exceeded' in line for line in logs.output))

    def test_full_worker_checks_deadlines_without_overlapping_safety_passes(self):
        class StopWorker(BaseException):
            pass

        timed, failed = self._enqueue('slow')
        start_attempt = pull._start_attempt
        wait_for_work = pull._wait_for_work
        tracked = {}
        passes = []
        limit = time.monotonic() + 8

        def start(pk, attempts):
            tracked['attempts'] = attempts
            if pk is None:
                self.assertFalse(any(attempt.pk is None for attempt in attempts.values()))
                if passes:
                    self.assertEqual(attempts, {})
                    raise StopWorker
                passes.append(time.monotonic())
            start_attempt(pk, attempts)

        def wait(delay):
            self.assertLess(time.monotonic(), limit, 'The worker did not finish its first safety pass.')
            wait_for_work(delay)

        try:
            with patch.object(pull, '_start_attempt', start), \
                    patch.object(pull, '_wait_for_work', wait), \
                    patch.object(pull, 'SAFETY_NET_SECONDS', 0.1):
                with self.assertRaises(StopWorker):
                    pull.run_worker(['django_logic.safety_net_test'], concurrency=1)
            self.assertEqual(len(passes), 1)
            self.assertEqual(tracked['attempts'], {})
            self._assert_outcomes(timed, failed)
            self.assertTrue((self.directory / 'callback_finished').exists())
        finally:
            for pid in tracked.get('attempts', {}):
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
