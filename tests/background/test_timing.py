"""Phase-2 timing fields on TransitionMessage.

Covers ``started_at`` / ``completed_at`` / ``duration_ms`` across the
happy path, terminal failure, retry, and the restore-failed bail-out.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from tests.background.models import Widget


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 0,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class HappyPathTimingTests(TestCase):
    def test_timing_fields_populated_on_success(self):
        widget = Widget.objects.create()
        before = timezone.now()
        widget.process.fulfil()
        after = timezone.now()

        tm = TransitionMessage.objects.get(instance_id=widget.pk)
        self.assertTrue(tm.is_completed)
        self.assertIsNotNone(tm.started_at)
        self.assertIsNotNone(tm.completed_at)
        self.assertIsNotNone(tm.duration_ms)
        self.assertGreaterEqual(tm.duration_ms, 0)
        # Bounds: start/complete both happened during this test.
        self.assertGreaterEqual(tm.started_at, before)
        self.assertLessEqual(tm.completed_at, after)
        self.assertGreaterEqual(tm.completed_at, tm.started_at)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class TerminalFailureTimingTests(TestCase):
    def test_timing_populated_when_hitting_max_errors(self):
        widget = Widget.objects.create()
        with override_settings(
            DJANGO_LOGIC=dict(_SYNC_SETTINGS, TRANSITION_MESSAGE_MAX_ERRORS=1)
        ):
            with self.assertRaises(ValueError):
                widget.process.crash()

        tm = TransitionMessage.objects.get(transition_name='crash')
        self.assertTrue(tm.is_completed)
        self.assertIsNotNone(tm.started_at)
        self.assertIsNotNone(tm.completed_at)
        self.assertIsNotNone(tm.duration_ms)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class NonTerminalFailureTimingTests(TestCase):
    def test_started_at_set_but_completed_at_null_mid_retry(self):
        widget = Widget.objects.create()
        with self.assertRaises(ValueError):
            widget.process.crash()

        tm = TransitionMessage.objects.get(transition_name='crash')
        self.assertFalse(tm.is_completed)
        self.assertIsNotNone(tm.started_at)
        self.assertIsNone(tm.completed_at)
        self.assertIsNone(tm.duration_ms)

    def test_started_at_is_overwritten_on_retry(self):
        widget = Widget.objects.create()
        with self.assertRaises(ValueError):
            widget.process.crash()

        tm = TransitionMessage.objects.get(transition_name='crash')
        self.assertIsNotNone(tm.started_at)

        # Simulate "time passed since the first attempt".
        stale = timezone.now() - timedelta(minutes=10)
        TransitionMessage.objects.filter(pk=tm.pk).update(started_at=stale)

        # Attempt 2. Call the runner directly to observe the raised
        # exception — retry_pending() swallows dispatch errors by design.
        with self.assertRaises(ValueError):
            run_background_transition(tm.pk)

        tm.refresh_from_db()
        self.assertIsNotNone(tm.started_at)
        self.assertGreater(tm.started_at, stale)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class RestoreFailedTimingTests(TestCase):
    def test_restore_failure_marks_completed_without_timing(self):
        widget = Widget.objects.create()
        # TM pointing at a transition that doesn't exist on the process —
        # _restore raises _RestoreError before mark_as_started runs.
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='nonexistent_transition',
            queue_name='django_logic.critical',
            kwargs={},
        )

        run_background_transition(tm.pk)

        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        # started_at should never have been set — phase 2 aborted before
        # mark_as_started. duration_ms stays null too.
        self.assertIsNone(tm.started_at)
        self.assertIsNone(tm.duration_ms)
        # completed_at is set so the row can be distinguished from
        # "never finished".
        self.assertIsNotNone(tm.completed_at)
