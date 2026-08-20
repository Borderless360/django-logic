"""Publishing for one row is bounded, and a queue with no consumer is named.

A row nothing consumes used to draw one broker message per starter tick,
forever, with no report. Now the starter claims ``last_dispatched_at``
before it publishes (at most one message per retry window), stops at the
dispatch ceiling for a row that never started, and
``detect_stuck_transitions`` reports that row and its queue.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic.background.models import TransitionMessage
from django_logic.background.tasks import (
    MAX_DISPATCHES_NEVER_STARTED,
    _retry_pending_inline,
    detect_stuck_transitions,
)
from django_logic.testing import open_transition_message
from tests.background.models import Widget
from tests import dl_settings


_APPLY_ASYNC = (
    'django_logic.background.tasks.run_background_transition_task.apply_async'
)

_CELERY_SETTINGS = dl_settings(
    BACKGROUND_EXECUTION='celery',
    TRANSITION_MESSAGE_MAX_ERRORS=3,
    TRANSITION_MESSAGE_RETRY_MINUTES=2,
)


def _never_started_row(widget, minutes_old=30):
    row = open_transition_message(
        widget, 'process', 'fulfil', queue_name='django_logic.critical',
    )
    past = timezone.now() - timedelta(minutes=minutes_old)
    TransitionMessage.objects.filter(pk=row.pk).update(created=past, modified=past)
    row.refresh_from_db()
    return row


@override_settings(DJANGO_LOGIC=_CELERY_SETTINGS)
class DispatchClaimTests(TestCase):
    def test_one_message_per_retry_window(self):
        widget = Widget.objects.create(status='fulfilling')
        row = _never_started_row(widget)
        with patch(_APPLY_ASYNC) as mock_async:
            self.assertEqual(_retry_pending_inline(), 1)
            self.assertEqual(_retry_pending_inline(), 0)
            self.assertEqual(_retry_pending_inline(), 0)
        self.assertEqual(mock_async.call_count, 1)
        row.refresh_from_db()
        self.assertEqual(row.dispatch_count, 1)
        self.assertIsNotNone(row.last_dispatched_at)

    def test_the_claim_reopens_after_the_window(self):
        widget = Widget.objects.create(status='fulfilling')
        row = _never_started_row(widget)
        with patch(_APPLY_ASYNC):
            _retry_pending_inline()
        TransitionMessage.objects.filter(pk=row.pk).update(
            last_dispatched_at=timezone.now() - timedelta(minutes=3),
        )
        with patch(_APPLY_ASYNC) as mock_async:
            self.assertEqual(_retry_pending_inline(), 1)
        self.assertEqual(mock_async.call_count, 1)
        row.refresh_from_db()
        self.assertEqual(row.dispatch_count, 2)

    def test_a_claim_does_not_touch_modified(self):
        widget = Widget.objects.create(status='fulfilling')
        row = _never_started_row(widget, minutes_old=60)
        before = row.modified
        with patch(_APPLY_ASYNC):
            _retry_pending_inline()
        row.refresh_from_db()
        self.assertEqual(row.modified, before)
        # So the row still reads as stranded — the classification the
        # missing-consumer scenario depends on.
        self.assertEqual(
            TransitionMessage.retry_status(widget, 'process'),
            TransitionMessage.STRANDED,
        )

    def test_a_failed_publish_gives_the_count_back(self):
        widget = Widget.objects.create(status='fulfilling')
        row = _never_started_row(widget)
        with patch(_APPLY_ASYNC, side_effect=RuntimeError('broker down')):
            self.assertEqual(_retry_pending_inline(), 0)
        row.refresh_from_db()
        # The window is spent (one ask per window against a broken broker),
        # but the ceiling counts only messages the broker really took.
        self.assertEqual(row.dispatch_count, 0)
        self.assertIsNotNone(row.last_dispatched_at)

    def test_the_primary_dispatch_counts_as_the_first(self):
        widget = Widget.objects.create(status='draft')
        with patch(_APPLY_ASYNC) as mock_async:
            with self.captureOnCommitCallbacks(execute=True):
                widget.process.fulfil()
        self.assertEqual(mock_async.call_count, 1)
        row = TransitionMessage.objects.get()
        self.assertEqual(row.dispatch_count, 1)
        # The starter finds the fresh claim and publishes nothing more.
        past = timezone.now() - timedelta(minutes=30)
        TransitionMessage.objects.filter(pk=row.pk).update(created=past)
        with patch(_APPLY_ASYNC) as mock_async:
            self.assertEqual(_retry_pending_inline(), 0)
        self.assertEqual(mock_async.call_count, 0)


@override_settings(DJANGO_LOGIC=_CELERY_SETTINGS)
class DispatchCeilingTests(TestCase):
    def test_the_starter_stops_at_the_ceiling(self):
        widget = Widget.objects.create(status='fulfilling')
        row = _never_started_row(widget)
        TransitionMessage.objects.filter(pk=row.pk).update(
            dispatch_count=MAX_DISPATCHES_NEVER_STARTED,
        )
        with patch(_APPLY_ASYNC) as mock_async:
            self.assertEqual(_retry_pending_inline(), 0)
        self.assertEqual(mock_async.call_count, 0)

    def test_detect_stuck_names_the_queue_and_finalizes_nothing(self):
        widget = Widget.objects.create(status='fulfilling')
        row = _never_started_row(widget)
        TransitionMessage.objects.filter(pk=row.pk).update(
            dispatch_count=MAX_DISPATCHES_NEVER_STARTED,
        )
        with self.assertLogs('django-logic', level='ERROR') as caught:
            self.assertEqual(detect_stuck_transitions(), 0)
        joined = '\n'.join(caught.output)
        self.assertIn('never started', joined)
        self.assertIn('django_logic.critical', joined)
        row.refresh_from_db()
        self.assertFalse(row.is_completed)

    def test_a_started_row_is_not_reported(self):
        widget = Widget.objects.create(status='fulfilling')
        open_transition_message(
            widget, 'process', 'fulfil', started_minutes_ago=5,
        )
        TransitionMessage.objects.update(
            dispatch_count=MAX_DISPATCHES_NEVER_STARTED,
        )
        with patch('django_logic.background.tasks.logger.error') as mock_error:
            detect_stuck_transitions()
        for call in mock_error.call_args_list:
            self.assertNotIn('never started', str(call))
