"""timeout= stops a hanging attempt: the worker parent kills the child.

The attempt holds its row lock while it runs, so nothing else can reach
it — the parent that forked it is the one place a budget can be
enforced. A killed attempt is accounted like a crashed one: one error on
the row, the claim's retry wait paces the next attempt, and MAX_ERRORS
ends it in ``failed_state`` (through the stuck finalizer).

Enforcement exists only where a child exists. In sync mode the attempt
runs in the caller's own thread and no budget is enforced.
"""
import os
import time
import unittest

from django.test import TransactionTestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.background.pull import _wait_for_child, run_once
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


@unittest.skipUnless(hasattr(os, 'fork'), 'needs os.fork')
class WaitForChildTests(TransactionTestCase):
    """The bounded wait itself, without a database row."""

    def test_a_child_inside_its_budget_exits_normally(self):
        child = os.fork()
        if child == 0:
            os._exit(0)
        exit_code, timed_out = _wait_for_child(child, 5)
        self.assertEqual(exit_code, 0)
        self.assertFalse(timed_out)

    def test_a_child_past_its_budget_is_killed(self):
        child = os.fork()
        if child == 0:
            time.sleep(30)
            os._exit(0)
        started = time.monotonic()
        exit_code, timed_out = _wait_for_child(child, 0.2)
        waited = time.monotonic() - started
        self.assertTrue(timed_out)
        self.assertNotEqual(exit_code, 0)
        self.assertLess(waited, 5.0, 'the kill did not bound the wait')

    def test_no_budget_means_an_unbounded_plain_wait(self):
        child = os.fork()
        if child == 0:
            os._exit(0)
        exit_code, timed_out = _wait_for_child(child, None)
        self.assertEqual(exit_code, 0)
        self.assertFalse(timed_out)


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
