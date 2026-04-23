"""Visibility of swallowed failure_side_effects errors on TransitionMessage."""
from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from tests.background.models import Widget


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 1,  # terminal on first attempt
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class FailureSideEffectErrorRecordingTests(TestCase):
    def test_broken_cleanup_recorded_on_tm(self):
        """When failure_side_effects raises, the swallowed error must
        land on the TM so operators can see the cleanup is broken."""
        widget = Widget.objects.create()
        with self.assertRaises(ValueError):
            widget.process.crash_with_bad_cleanup()

        tm = TransitionMessage.objects.get(
            transition_name='crash_with_bad_cleanup'
        )
        # The row was terminated (MAX_ERRORS=1) and failed_state was written.
        self.assertTrue(tm.is_completed)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'cwbc_failed')

        # The original side-effect error sits in last_error_message.
        self.assertEqual(tm.last_error_message, 'boom')
        # The swallowed cleanup error is now visible in its own field.
        self.assertIn('RuntimeError', tm.failure_side_effect_error)
        self.assertIn('cleanup exploded', tm.failure_side_effect_error)

    def test_no_cleanup_error_leaves_field_empty(self):
        """``crash`` has no failure_side_effects — the field stays empty."""
        widget = Widget.objects.create()
        with self.assertRaises(ValueError):
            widget.process.crash()

        tm = TransitionMessage.objects.get(transition_name='crash')
        self.assertEqual(tm.failure_side_effect_error, '')
