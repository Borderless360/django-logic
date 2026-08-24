"""timeout= stops a hanging attempt: the worker kills the attempt process.

The attempt holds its row lock while it runs, so nothing else can reach
it — the worker that forked it is the one place a budget can be
enforced. A killed attempt is accounted like a crashed one: one error on
the row, the claim's retry wait paces the next attempt, and MAX_ERRORS
ends it in ``failed_state`` (through the stuck finalizer).

Enforcement exists only where an attempt process exists. In sync mode
the attempt runs in the caller's own thread and no budget is enforced.

What the worker records is the whole contract, so these tests watch
``_record_attempt_error`` rather than a return value.
"""
import os
import signal
import time
import unittest
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.background.pull import _Attempt, _harvest, run_once
from django_logic.testing import open_transition_message
from tests.background.models import Widget
from tests.stability.base import requires_postgres
from tests import dl_settings


_PULL_SETTINGS = dl_settings(
    BACKGROUND_EXECUTION='pull',
    TRANSITION_MESSAGE_MAX_ERRORS=3,
    TRANSITION_MESSAGE_RETRY_MINUTES=2,
)

_CRITICAL = ['django_logic.critical']

#: A deadline this far in the past has always passed.
_PASSED = 0.0


@unittest.skipUnless(hasattr(os, 'fork'), 'needs os.fork')
class HarvestTests(TransactionTestCase):
    """The bounded wait itself, without a database row."""

    def setUp(self):
        patcher = patch('django_logic.background.pull._record_attempt_error')
        self.record = patcher.start()
        self.addCleanup(patcher.stop)

    def recorded(self):
        return [call.args[1] for call in self.record.call_args_list]

    @staticmethod
    def _attempt(pid, *, timeout_seconds=None, deadline=None):
        return {pid: _Attempt(
            pk=1, timeout_seconds=timeout_seconds, deadline=deadline)}

    def test_an_attempt_process_inside_its_budget_is_charged_nothing(self):
        attempt_pid = os.fork()
        if attempt_pid == 0:
            os._exit(0)
        attempts = self._attempt(
            attempt_pid, timeout_seconds=5,
            deadline=time.monotonic() + 5,
        )
        _harvest(attempts, block=True)
        self.assertEqual(attempts, {})
        self.assertEqual(self.recorded(), [])

    def test_an_attempt_process_past_its_budget_is_killed_and_charged(self):
        attempt_pid = os.fork()
        if attempt_pid == 0:
            time.sleep(30)
            os._exit(0)
        attempts = self._attempt(
            attempt_pid, timeout_seconds=0.2, deadline=_PASSED)
        started = time.monotonic()
        _harvest(attempts, block=True)
        waited = time.monotonic() - started

        self.assertLess(waited, 5.0, 'the kill did not bound the wait')
        self.assertEqual(len(self.recorded()), 1)
        self.assertIn('[timeout]', self.recorded()[0])

    def test_a_budget_is_enforced_while_the_attempt_keeps_running(self):
        """The deadline has not passed yet, so the kill has to come from
        the poll rather than from the first pass."""
        attempt_pid = os.fork()
        if attempt_pid == 0:
            time.sleep(30)
            os._exit(0)
        attempts = self._attempt(
            attempt_pid, timeout_seconds=0.3,
            deadline=time.monotonic() + 0.3,
        )
        started = time.monotonic()
        _harvest(attempts, block=True)
        waited = time.monotonic() - started

        self.assertGreaterEqual(waited, 0.3)
        self.assertLess(waited, 5.0, 'the kill did not bound the wait')
        self.assertIn('[timeout]', self.recorded()[0])

    def test_no_budget_means_an_unbounded_plain_wait(self):
        attempt_pid = os.fork()
        if attempt_pid == 0:
            os._exit(0)
        attempts = self._attempt(attempt_pid)
        _harvest(attempts, block=True)
        self.assertEqual(self.recorded(), [])

    def test_an_attempt_process_that_dies_is_charged_a_crash(self):
        attempt_pid = os.fork()
        if attempt_pid == 0:
            os._exit(3)
        attempts = self._attempt(attempt_pid)
        _harvest(attempts, block=True)
        self.assertEqual(len(self.recorded()), 1)
        self.assertIn('[crashed]', self.recorded()[0])
        self.assertIn('exit 3', self.recorded()[0])

    def test_a_process_that_ended_before_the_kill_is_charged_nothing(self):
        """``os.kill`` succeeds on a process that has exited and is
        waiting to be reaped, so the kill alone must not decide the
        outcome — the status the reap returns does."""
        attempts = self._attempt(12345, timeout_seconds=1, deadline=_PASSED)
        with patch('django_logic.background.pull.os.waitpid',
                   return_value=(12345, 0)), \
                patch('django_logic.background.pull.os.kill',
                      return_value=None), \
                patch('django_logic.background.pull.os.waitstatus_to_exitcode',
                      return_value=0):
            _harvest(attempts, block=True)
        self.assertEqual(self.recorded(), [])

    def test_an_attempt_that_vanished_before_the_kill_does_not_stop_the_worker(self):
        """``os.kill`` raises ProcessLookupError when the attempt is
        already gone. The worker must reap and carry on."""
        attempts = self._attempt(12345, timeout_seconds=1, deadline=_PASSED)
        with patch('django_logic.background.pull.os.waitpid',
                   return_value=(12345, 0)), \
                patch('django_logic.background.pull.os.kill',
                      side_effect=ProcessLookupError), \
                patch('django_logic.background.pull.os.waitstatus_to_exitcode',
                      return_value=0):
            _harvest(attempts, block=True)
        self.assertEqual(self.recorded(), [])

    def test_a_killed_attempt_reaped_elsewhere_is_still_a_timeout(self):
        attempts = self._attempt(12345, timeout_seconds=1, deadline=_PASSED)
        with patch('django_logic.background.pull.os.waitpid',
                   side_effect=ChildProcessError), \
                patch('django_logic.background.pull.os.kill',
                      return_value=None):
            _harvest(attempts, block=True)
        self.assertEqual(attempts, {})
        self.assertIn('[timeout]', self.recorded()[0])

    def test_an_attempt_reaped_elsewhere_inside_its_budget_is_charged_nothing(self):
        attempts = self._attempt(
            12345, timeout_seconds=60, deadline=time.monotonic() + 60)
        with patch('django_logic.background.pull.os.waitpid',
                   side_effect=ChildProcessError):
            _harvest(attempts, block=True)
        self.assertEqual(attempts, {})
        self.assertEqual(self.recorded(), [])

    def test_a_kill_reported_as_a_signal_exit_is_a_timeout(self):
        attempts = self._attempt(12345, timeout_seconds=1, deadline=_PASSED)
        with patch('django_logic.background.pull.os.waitpid',
                   return_value=(12345, 0)), \
                patch('django_logic.background.pull.os.kill',
                      return_value=None), \
                patch('django_logic.background.pull.os.waitstatus_to_exitcode',
                      return_value=-signal.SIGKILL):
            _harvest(attempts, block=True)
        self.assertIn('[timeout]', self.recorded()[0])

    def test_one_budget_does_not_bound_the_attempt_beside_it(self):
        """Several attempts run at a time. Each one carries its own
        budget, and the worker accounts for each on its own terms."""
        hanging_pid = os.fork()
        if hanging_pid == 0:
            time.sleep(30)
            os._exit(0)
        clean_pid = os.fork()
        if clean_pid == 0:
            os._exit(0)

        attempts = {
            hanging_pid: _Attempt(
                pk=1, timeout_seconds=0.2, deadline=_PASSED),
            clean_pid: _Attempt(pk=2, timeout_seconds=None, deadline=None),
        }
        while attempts:
            _harvest(attempts, block=True)

        self.assertEqual(len(self.recorded()), 1)
        self.assertIn('[timeout]', self.recorded()[0])
        self.assertEqual(self.record.call_args_list[0].args[0], 1)

    def test_harvesting_nothing_returns_at_once(self):
        attempts = {}
        _harvest(attempts, block=True)
        self.assertEqual(self.recorded(), [])


@override_settings(DJANGO_LOGIC=_PULL_SETTINGS)
@requires_postgres
class TimeoutKillTests(TransactionTestCase):
    """End to end: a hanging attempt with timeout= is stopped and charged."""

    databases = '__all__'

    def test_a_hanging_attempt_is_killed_and_charged_once(self):
        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(
            widget, 'process', 'hang', queue_name='django_logic.critical',
        )
        TransitionMessage.objects.filter(pk=row.pk).update(timeout_seconds=1)

        started = time.monotonic()
        self.assertTrue(run_once(_CRITICAL, isolate=True))
        waited = time.monotonic() - started

        self.assertLess(waited, 15.0, 'the attempt was not stopped at its budget')
        row.refresh_from_db()
        self.assertFalse(row.is_completed)
        self.assertEqual(row.errors_count, 1)
        self.assertIn('[timeout]', row.last_error_message)
        # The instance is untouched: the killed attempt rolled back.
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilling')
