"""Timing fields the worker stamps on TransitionMessage.

Covers ``started_at`` / ``completed_at`` / ``duration_ms`` on the happy path,
on terminal failure, on retry, and when the transition cannot be restored.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from tests.background.models import Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3, TRANSITION_MESSAGE_RETRY_MINUTES=0)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class HappyPathTimingTests(TestCase):
    def test_timing_fields_populated_on_success(self):
        widget = Widget.objects.create()
        before = timezone.now()
        widget.process.fulfil()
        after = timezone.now()

        transition_message = TransitionMessage.objects.get(
            instance_id=widget.pk)
        self.assertTrue(transition_message.is_completed)
        self.assertIsNotNone(transition_message.started_at)
        self.assertIsNotNone(transition_message.completed_at)
        self.assertIsNotNone(transition_message.duration_ms)
        self.assertGreaterEqual(transition_message.duration_ms, 0)
        # Both timestamps fall inside this test.
        self.assertGreaterEqual(transition_message.started_at, before)
        self.assertLessEqual(transition_message.completed_at, after)
        self.assertGreaterEqual(transition_message.completed_at,
                                transition_message.started_at)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class TerminalFailureTimingTests(TestCase):
    def test_timing_populated_when_hitting_max_errors(self):
        widget = Widget.objects.create()
        with override_settings(
            DJANGO_LOGIC=dict(_SYNC_SETTINGS, TRANSITION_MESSAGE_MAX_ERRORS=1)
        ):
            with self.assertRaises(ValueError):
                widget.process.crash()

        transition_message = TransitionMessage.objects.get(
            transition_name='crash')
        self.assertTrue(transition_message.is_completed)
        self.assertIsNotNone(transition_message.started_at)
        self.assertIsNotNone(transition_message.completed_at)
        self.assertIsNotNone(transition_message.duration_ms)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class NonTerminalFailureTimingTests(TestCase):
    def test_started_at_set_but_completed_at_null_mid_retry(self):
        widget = Widget.objects.create()
        with self.assertRaises(ValueError):
            widget.process.crash()

        transition_message = TransitionMessage.objects.get(
            transition_name='crash')
        self.assertFalse(transition_message.is_completed)
        self.assertIsNotNone(transition_message.started_at)
        self.assertIsNone(transition_message.completed_at)
        self.assertIsNone(transition_message.duration_ms)

    def test_started_at_is_overwritten_on_retry(self):
        widget = Widget.objects.create()
        with self.assertRaises(ValueError):
            widget.process.crash()

        transition_message = TransitionMessage.objects.get(
            transition_name='crash')
        self.assertIsNotNone(transition_message.started_at)

        # Pretend time passed since the first attempt.
        stale = timezone.now() - timedelta(minutes=10)
        TransitionMessage.objects.filter(
            pk=transition_message.pk).update(started_at=stale)

        # Second attempt. Call the runner directly to see the exception:
        # retry_pending() swallows dispatch errors by design.
        with self.assertRaises(ValueError):
            run_background_transition(transition_message.pk)

        transition_message.refresh_from_db()
        self.assertIsNotNone(transition_message.started_at)
        self.assertGreater(transition_message.started_at, stale)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class RestoreFailedTimingTests(TestCase):
    def test_restore_failure_marks_completed_without_timing(self):
        widget = Widget.objects.create()
        # The row names a transition the process does not have, so the restore
        # step fails before any work runs.
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='nonexistent_transition',
            queue_name='django_logic.critical',
            kwargs={},
        )

        run_background_transition(transition_message.pk)

        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        # started_at is stamped and committed before the restore, so the
        # watchdog can see a hung or crashed attempt. duration_ms stays null
        # because no work was measured.
        self.assertIsNotNone(transition_message.started_at)
        self.assertIsNone(transition_message.duration_ms)
        # completed_at is set so the row does not read as never finished.
        self.assertIsNotNone(transition_message.completed_at)
