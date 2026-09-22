"""A full worker reuses finished attempt slots without waiting for new work."""
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

from django.db import connections
from django.test import TransactionTestCase, override_settings

from django_logic import Process
from django_logic.background import BackgroundTransition, pull
from django_logic.background.models import TransitionMessage
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres


_QUEUE = 'django_logic.slot_reuse_test'


def _write_event(directory, name):
    destination = Path(directory) / f'{name}-{os.getpid()}.json'
    temporary = destination.with_suffix('.pending')
    temporary.write_text(json.dumps({'pid': os.getpid(), 'time': time.monotonic()}))
    temporary.replace(destination)


def _short_work(instance, marker_directory, job_name, sleep_seconds=0.1,
                wait_for_maintenance=False, **kwargs):
    _write_event(marker_directory, f'started-{job_name}')
    if wait_for_maintenance:
        deadline = time.monotonic() + 4
        while len(list(Path(marker_directory).glob('maintenance-*.json'))) < 2:
            if time.monotonic() >= deadline:
                raise RuntimeError('The full worker did not run its next maintenance pass.')
            time.sleep(0.01)
    else:
        time.sleep(sleep_seconds)


def _finished_work(instance, marker_directory, job_name, **kwargs):
    # Success callbacks run after the state and completion writes commit.
    _write_event(marker_directory, f'finished-{job_name}')


class SlotReuseProcess(Process):
    process_name = 'slot_reuse_test'
    transitions = [
        BackgroundTransition(
            'work', sources=['draft'], target='fulfilled',
            in_progress_state='fulfilling', failed_state='failed',
            queue=_QUEUE, side_effects=[_short_work], callbacks=[_finished_work],
        ),
    ]


class _WorkerStopped(BaseException):
    pass


class _WorkerDeadline(BaseException):
    pass


@requires_postgres
@unittest.skipUnless(hasattr(os, 'fork'), 'needs os.fork')
@override_settings(DJANGO_LOGIC=dl_settings(
    BACKGROUND_EXECUTION='pull',
    TRANSITION_MESSAGE_MAX_ERRORS=3,
    TRANSITION_MESSAGE_RETRY_MINUTES=2,
))
class WorkerSlotReuseTests(TransactionTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='dl_slot_reuse_')
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.widgets = []

    def _enqueue(self, job_name, **kwargs):
        widget = Widget.objects.create(status='draft')
        SlotReuseProcess(instance=widget, field_name='status').work(
            marker_directory=str(self.directory), job_name=job_name, **kwargs,
        )
        self.widgets.append(widget)

    def _event(self, kind, job_name):
        paths = list(self.directory.glob(f'{kind}-{job_name}-*.json'))
        self.assertEqual(len(paths), 1, f'{kind} must run once for {job_name}')
        return json.loads(paths[0].read_text())

    def _run_worker(self, concurrency):
        claim_next = pull.claim_next
        start_attempt = pull._start_attempt
        tracked = {}
        child_pids = []

        def start(pk, attempts):
            tracked['attempts'] = attempts
            before = set(attempts)
            start_attempt(pk, attempts)
            child_pids.extend(set(attempts) - before)

        def claim(*args, **kwargs):
            # End only after the real loop has reaped every child it started.
            # Failed attempts also stop here, so their assertions fail promptly.
            if (not tracked.get('attempts') and not TransitionMessage.objects.filter(
                    queue_name=_QUEUE, is_completed=False, errors_count=0).exists()):
                raise _WorkerStopped
            return claim_next(*args, **kwargs)

        def expired(signum, frame):
            raise _WorkerDeadline('The worker did not stop within twelve seconds.')

        # Enqueue notifications precede this fresh LISTEN connection. No producer
        # wakes the worker while it drains the already committed backlog.
        connections.close_all()
        old_handler = signal.signal(signal.SIGALRM, expired)
        old_timer = signal.setitimer(signal.ITIMER_REAL, 12)
        try:
            with patch.object(pull, '_start_attempt', start), \
                    patch.object(pull, 'claim_next', claim):
                with self.assertRaises(_WorkerStopped):
                    pull.run_worker([_QUEUE], forever=True, concurrency=concurrency)
            for pid in child_pids:
                with self.assertRaises(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
        finally:
            signal.setitimer(signal.ITIMER_REAL, *old_timer)
            signal.signal(signal.SIGALRM, old_handler)
            for pid in child_pids:
                try:
                    child, _ = os.waitpid(pid, os.WNOHANG)
                    if not child:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass

        rows = TransitionMessage.objects.filter(queue_name=_QUEUE)
        self.assertEqual(rows.count(), len(self.widgets))
        self.assertFalse(rows.filter(is_completed=False).exists())
        self.assertFalse(rows.exclude(errors_count=0).exists())
        for widget in self.widgets:
            widget.refresh_from_db()
            self.assertEqual(widget.status, 'fulfilled')

    def _assert_prompt_reuse(self, preceding, following):
        completed = self._event('finished', preceding)
        started = self._event('started', following)
        gap = started['time'] - completed['time']
        self.assertGreaterEqual(gap, 0)
        self.assertLess(gap, 0.75, f'{following} waited {gap:.3f}s for a free slot')

    def test_one_slot_drains_short_jobs_without_new_notifications(self):
        for job_name in ('first', 'second', 'third'):
            self._enqueue(job_name)
        # A larger real poll interval separates a missed child exit from noise.
        with patch.object(pull, 'BUSY_POLL_SECONDS', 2):
            self._run_worker(concurrency=1)
        self._assert_prompt_reuse('first', 'second')
        self._assert_prompt_reuse('second', 'third')
        for job_name in ('first', 'second', 'third'):
            self.assertEqual(self._event('started', job_name)['pid'],
                             self._event('finished', job_name)['pid'])

    def test_finished_slot_takes_backlog_while_a_sibling_keeps_running(self):
        self._enqueue('slow', sleep_seconds=3)
        self._enqueue('first')
        self._enqueue('second')
        self._enqueue('third')
        with patch.object(pull, 'BUSY_POLL_SECONDS', 2):
            self._run_worker(concurrency=2)
        self._assert_prompt_reuse('first', 'second')
        self._assert_prompt_reuse('second', 'third')
        self.assertLess(self._event('started', 'third')['time'],
                        self._event('finished', 'slow')['time'])
        for job_name in ('slow', 'first', 'second', 'third'):
            self.assertEqual(self._event('started', job_name)['pid'],
                             self._event('finished', job_name)['pid'])

    def test_a_full_worker_runs_maintenance_before_its_attempt_finishes(self):
        self._enqueue('waiting', wait_for_maintenance=True)

        def record_maintenance():
            _write_event(self.directory, 'maintenance')

        with patch.object(pull, '_run_safety_nets', record_maintenance), \
                patch.object(pull, 'SAFETY_NET_SECONDS', 0.4):
            self._run_worker(concurrency=1)

        passes = sorted(json.loads(path.read_text())['time']
                        for path in self.directory.glob('maintenance-*.json'))
        self.assertGreaterEqual(len(passes), 2)
        self.assertLess(self._event('started', 'waiting')['time'], passes[1])
        self.assertLess(passes[1], self._event('finished', 'waiting')['time'])
