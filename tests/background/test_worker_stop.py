"""A worker that gets SIGTERM finishes its running job, takes no new job, and exits.

A platform sends SIGTERM before it replaces a worker. Heroku sends it to every
process of the dyno. Kubernetes sends it to the worker process only.
"""
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest

from django.core.management import call_command
from django.db import connections
from django.test import TransactionTestCase, override_settings

from django_logic import Process
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres


_QUEUE = 'django_logic.worker_stop_test'


def _work(instance, marker, seconds, **kwargs):
    Path(marker).touch()
    time.sleep(seconds)
    instance.se_log += 'saved,'
    instance.save(update_fields=['se_log'])


class WorkerStopProcess(Process):
    process_name = 'worker_stop_test'
    transitions = [
        BackgroundTransition(
            'work', sources=['draft'], target='fulfilled',
            in_progress_state='fulfilling', failed_state='failed',
            queue=_QUEUE, side_effects=[_work],
        ),
    ]


@requires_postgres
@unittest.skipUnless(hasattr(os, 'fork'), 'needs os.fork')
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class WorkerStopTests(TransactionTestCase):
    """Real commits and real processes: the signal reaches a worker that runs a job."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='dl_worker_stop_')
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def _enqueue(self, name, seconds):
        widget = Widget.objects.create(status='draft')
        marker = self.directory / name
        WorkerStopProcess(instance=widget, field_name='status').work(marker=str(marker), seconds=seconds)
        return widget, marker

    def _job(self, widget):
        return TransitionMessage.objects.get(instance_id=str(widget.pk), queue_name=_QUEUE)

    def _start_worker(self):
        connections.close_all()
        worker_pid = os.fork()
        if worker_pid == 0:
            os.setsid()
            status = 1
            try:
                call_command('dl_worker', queues=_QUEUE, concurrency=1)
                status = 0
            finally:
                os._exit(status)
        self.addCleanup(self._kill, worker_pid)
        return worker_pid

    @staticmethod
    def _kill(worker_pid):
        try:
            os.killpg(worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(worker_pid, 0)
        except ChildProcessError:
            pass

    def _wait_until(self, done, what, seconds=10):
        deadline = time.monotonic() + seconds
        while not done():
            self.assertLess(time.monotonic(), deadline, f'{what} did not happen within {seconds} seconds')
            time.sleep(0.02)

    def _exit_code(self, worker_pid, seconds):
        deadline = time.monotonic() + seconds
        while True:
            pid, raw_status = os.waitpid(worker_pid, os.WNOHANG)
            if pid:
                return os.waitstatus_to_exitcode(raw_status)
            self.assertLess(time.monotonic(), deadline, f'The worker did not exit within {seconds} seconds')
            time.sleep(0.05)

    def _stop_during_a_job(self, send_signal):
        running, marker = self._enqueue('running', seconds=2)
        waiting, _ = self._enqueue('waiting', seconds=0)
        worker_pid = self._start_worker()
        self._wait_until(marker.exists, 'The start of the job')

        send_signal(worker_pid)

        self.assertEqual(self._exit_code(worker_pid, seconds=10), 0)
        running.refresh_from_db()
        waiting.refresh_from_db()
        job = self._job(running)
        self.assertEqual((running.status, running.se_log), ('fulfilled', 'saved,'))
        self.assertTrue(job.is_completed)
        self.assertEqual((job.errors_count, job.start_attempt_count), (0, 1))
        self.assertEqual((waiting.status, waiting.se_log), ('fulfilling', ''))
        self.assertEqual(self._job(waiting).start_attempt_count, 0)

    def test_signal_to_every_process_lets_the_running_job_finish(self):
        self._stop_during_a_job(lambda worker_pid: os.killpg(worker_pid, signal.SIGTERM))

    def test_signal_to_the_worker_process_alone_lets_the_running_job_finish(self):
        self._stop_during_a_job(lambda worker_pid: os.kill(worker_pid, signal.SIGTERM))

    def test_worker_without_a_job_exits(self):
        done, _ = self._enqueue('done', seconds=0)
        worker_pid = self._start_worker()
        self._wait_until(lambda: self._job(done).is_completed, 'The end of the job')

        os.killpg(worker_pid, signal.SIGTERM)

        self.assertEqual(self._exit_code(worker_pid, seconds=10), 0)
