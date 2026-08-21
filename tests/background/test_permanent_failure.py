"""A permanent failure takes the terminal path on the first attempt.

Two ways to say it: raise ``PermanentFailure`` from the side-effect, or
declare ``no_retry_on=(SomeError, ...)`` on the transition. Both must
write ``failed_state``, run ``failure_callbacks``, and complete the row
without waiting out ``MAX_ERRORS``.
"""
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from django_logic.background import BackgroundTransition, PermanentFailure
from django_logic.background.models import TransitionMessage
from tests.background.models import Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class PermanentFailureTests(TestCase):
    def test_raised_permanent_failure_is_terminal_on_the_first_attempt(self):
        widget = Widget.objects.create(status='draft')
        with self.assertRaises(PermanentFailure):
            widget.process.refuse()
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'refused')
        # fcb = the failure callback ran; see bg_failure_callback.
        self.assertIn('fcb', widget.cb_log)
        row = TransitionMessage.objects.get(transition_name='refuse')
        self.assertTrue(row.is_completed)
        self.assertEqual(row.errors_count, 1)
        self.assertIn('the rule says no', row.last_error_message)

    def test_declared_exception_type_is_terminal_on_the_first_attempt(self):
        widget = Widget.objects.create(status='draft')
        with self.assertRaises(ValueError):
            widget.process.refuse_declared()
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'rd_refused')
        self.assertIn('fcb', widget.cb_log)
        row = TransitionMessage.objects.get(transition_name='refuse_declared')
        self.assertTrue(row.is_completed)
        self.assertEqual(row.errors_count, 1)

    def test_an_ordinary_failure_still_retries(self):
        widget = Widget.objects.create(status='draft')
        with self.assertRaises(ValueError):
            widget.process.crash()
        row = TransitionMessage.objects.get(transition_name='crash')
        self.assertFalse(row.is_completed)
        self.assertEqual(row.errors_count, 1)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'crashing')

    def test_no_retry_on_accepts_a_single_exception_type(self):
        transition = BackgroundTransition(
            'one', sources=['a'], target='b', no_retry_on=ValueError,
        )
        self.assertEqual(transition.no_retry_on, (ValueError,))

    def test_no_retry_on_rejects_a_non_exception(self):
        with self.assertRaises(ImproperlyConfigured):
            BackgroundTransition(
                'bad', sources=['a'], target='b', no_retry_on=('boom',),
            )
