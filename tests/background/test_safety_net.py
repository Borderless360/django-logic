"""The three periodic tasks that keep rows moving: retry, cleanup, detect stuck."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic.background import retry_pending
from django_logic.background.models import TransitionMessage
from django_logic.background.safety_nets import (
    cleanup_completed_transitions,
    detect_stuck_transitions,
    retry_pending,
)
from django_logic.testing import open_transition_message
from tests.background.models import Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3, TRANSITION_MESSAGE_RETRY_MINUTES=0)


def _make_stale_message(widget, transition_name='fulfil', errors=0, completed=False):
    transition_message = TransitionMessage.objects.create(
        app_label='bg_tests',
        model_name='widget',
        instance_id=widget.pk,
        process_name='process',
        transition_name=transition_name,
        queue_name='django_logic.critical',
        kwargs={},
        errors_count=errors,
        is_completed=completed,
    )
    # Move the timestamps back so the RETRY_MINUTES filter includes the row.
    TransitionMessage.objects.filter(pk=transition_message.pk).update(
        created=timezone.now() - timedelta(minutes=5),
        modified=timezone.now() - timedelta(minutes=5),
    )
    transition_message.refresh_from_db()
    return transition_message


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class RetryStaleTests(TestCase):
    def test_picks_up_uncompleted_message(self):
        widget = Widget.objects.create(status='fulfilling')
        _make_stale_message(widget)
        dispatched = retry_pending()
        self.assertEqual(dispatched, 1)
        widget.refresh_from_db()
        # Execute ran inline, so the widget reached its target state.
        self.assertEqual(widget.status, 'fulfilled')

    def test_skips_completed(self):
        widget = Widget.objects.create(status='fulfilled')
        _make_stale_message(widget, completed=True)
        self.assertEqual(retry_pending(), 0)

    def test_stops_at_max_errors(self):
        widget = Widget.objects.create(status='fulfilling')
        _make_stale_message(widget, errors=99)  # above MAX_ERRORS, which is 3
        self.assertEqual(retry_pending(), 0)

    def test_retry_pending_helper_is_public(self):
        widget = Widget.objects.create(status='fulfilling')
        _make_stale_message(widget)
        self.assertEqual(retry_pending(), 1)

    def test_deletes_old_completed_messages(self):
        widget = Widget.objects.create()
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='fulfil',
            queue_name='q',
            is_completed=True,
        )
        TransitionMessage.objects.filter(pk=transition_message.pk).update(
            modified=timezone.now() - timedelta(days=30)
        )
        deleted = cleanup_completed_transitions()
        self.assertEqual(deleted, 1)

    def test_preserves_uncompleted_messages(self):
        widget = Widget.objects.create()
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='fulfil',
            queue_name='q',
            is_completed=False,
        )
        TransitionMessage.objects.filter(pk=transition_message.pk).update(
            modified=timezone.now() - timedelta(days=30)
        )
        self.assertEqual(cleanup_completed_transitions(), 0)
        self.assertTrue(
            TransitionMessage.objects.filter(pk=transition_message.pk).exists())


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class DetectStuckTests(TestCase):
    def test_finalizes_message_at_max_errors(self):
        """A row stuck at MAX_ERRORS is forced terminal. 'fulfil' declares a
        failed_state, so it is written, and the row is marked completed so the
        retry loop stops picking it up."""
        widget = Widget.objects.create(status='fulfilling')
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='fulfil',
            queue_name='q',
            errors_count=3,
        )
        self.assertEqual(detect_stuck_transitions(), 1)

        transition_message = TransitionMessage.objects.get(instance_id=widget.pk)
        self.assertTrue(transition_message.is_completed)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilment_failed')

    def test_no_failed_state_marks_completed_without_state_change(self):
        """When the transition declares no failed_state, the row is marked
        completed and the instance is left in progress for an operator."""
        widget = Widget.objects.create(status='fulfilling')
        # sync_inventory is a BackgroundTransition with no failed_state.
        widget.status = 'fulfilled'
        widget.save(update_fields=['status'])
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='sync_inventory',
            queue_name='q',
            errors_count=3,
        )
        self.assertEqual(detect_stuck_transitions(), 1)

        transition_message = TransitionMessage.objects.get(instance_id=widget.pk)
        self.assertTrue(transition_message.is_completed)
        widget.refresh_from_db()
        # Unchanged: there is no failed_state to write.
        self.assertEqual(widget.status, 'fulfilled')

    def test_idempotent_on_completed_rows(self):
        widget = Widget.objects.create()
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='fulfil',
            queue_name='q',
            errors_count=3,
            is_completed=True,
        )
        self.assertEqual(detect_stuck_transitions(), 0)

    def test_undecodable_kwargs_row_still_finalized(self):
        """A stuck row whose kwargs no longer decode is still forced terminal,
        with empty kwargs. A decode failure must not stop the safety net and
        leave the retry loop picking the row up forever."""
        widget = Widget.objects.create(status='fulfilling')
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='fulfil',
            queue_name='q',
            kwargs={'user_id': ['not', 'a', 'pk']},
            errors_count=3,
        )
        self.assertEqual(detect_stuck_transitions(), 1)
        transition_message = TransitionMessage.objects.get(instance_id=widget.pk)
        self.assertTrue(transition_message.is_completed)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilment_failed')

    def test_unrestorable_row_still_marked_completed(self):
        """A stuck row that names a transition which no longer exists is still
        forced terminal, so the retry loop stops."""
        widget = Widget.objects.create(status='fulfilling')
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='nonexistent_transition',
            queue_name='q',
            errors_count=3,
        )
        self.assertEqual(detect_stuck_transitions(), 1)
        transition_message = TransitionMessage.objects.get(instance_id=widget.pk)
        self.assertTrue(transition_message.is_completed)

    def test_finalize_runs_failure_callbacks_and_nulls_duration(self):
        """A row the safety net finalizes runs failure_callbacks, just as a
        worker attempt does. It records no duration_ms, because the abandoned
        attempt's started_at would give a wrong figure."""
        widget = Widget.objects.create(status='fulfilling')
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(widget.pk),
            process_name='process',
            transition_name='fulfil',  # declares failed_state + failure_callbacks
            queue_name='q',
            errors_count=3,
        )
        # An abandoned attempt that started 5 minutes ago.
        TransitionMessage.objects.filter(pk=transition_message.pk).update(
            started_at=timezone.now() - timedelta(minutes=5),
        )

        self.assertEqual(detect_stuck_transitions(), 1)

        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilment_failed')
        self.assertIn('fcb,', widget.cb_log)  # the failure_callbacks ran

        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertIsNone(transition_message.duration_ms)  # no attempt finished


_REPORT_SETTINGS = dl_settings(
    TRANSITION_MESSAGE_MAX_ERRORS=3, TRANSITION_MESSAGE_RETRY_MINUTES=2,
)


def _waiting_row(queue_name, minutes):
    """A row on ``queue_name`` that no attempt started, created ``minutes`` ago."""
    widget = Widget.objects.create(status='fulfilling')
    row = open_transition_message(
        widget, 'process', 'fulfil', queue_name=queue_name)
    TransitionMessage.objects.filter(pk=row.pk).update(
        created=timezone.now() - timedelta(minutes=minutes))
    return row


def _attempt(queue_name, started_minutes_ago):
    """A row on ``queue_name`` whose attempt started that long ago."""
    widget = Widget.objects.create(status='fulfilling')
    return open_transition_message(
        widget, 'process', 'fulfil', queue_name=queue_name,
        started_minutes_ago=started_minutes_ago)


@override_settings(DJANGO_LOGIC=_REPORT_SETTINGS)
class WaitingRowReportTests(TestCase):
    """These settings give a 15-minute retry window. The report writes one
    line per queue, and an ERROR only when no worker takes rows from it."""

    def _report(self):
        with self.assertLogs('django-logic', level='WARNING') as logs:
            detect_stuck_transitions()
        return logs.records

    def test_a_queue_no_worker_takes_rows_from_is_one_error(self):
        oldest = _waiting_row('nobody_serves_this', 40)
        _waiting_row('nobody_serves_this', 30)
        _waiting_row('nobody_serves_this', 20)
        records = self._report()
        self.assertEqual([record.levelname for record in records], ['ERROR'])
        message = records[0].getMessage()
        self.assertIn(
            f'TransitionMessage#{oldest.pk} has waited 40 minutes', message)
        self.assertIn('2 more rows wait behind it', message)
        self.assertIn('dl_worker --queues nobody_serves_this', message)

    def test_a_backlog_on_a_queue_a_worker_takes_rows_from_is_one_warning(self):
        for minutes in (40, 30, 20):
            _waiting_row('busy', minutes)
        _attempt('busy', started_minutes_ago=1)
        records = self._report()
        self.assertEqual([record.levelname for record in records], ['WARNING'])
        message = records[0].getMessage()
        self.assertIn("queue 'busy' is behind", message)
        self.assertIn('3 rows have waited', message)

    def test_a_long_attempt_a_worker_holds_keeps_its_queue_served(self):
        _waiting_row('slow', 30)
        long_attempt = _attempt('slow', started_minutes_ago=25)
        with patch.object(
            TransitionMessage, 'worker_holds_row', return_value=True,
        ) as probe:
            records = self._report()
        probe.assert_called_with(long_attempt.pk)
        self.assertEqual([record.levelname for record in records], ['WARNING'])

    def test_an_old_attempt_no_worker_holds_does_not_serve_the_queue(self):
        _waiting_row('slow', 30)
        _attempt('slow', started_minutes_ago=25)
        records = self._report()
        self.assertEqual([record.levelname for record in records], ['ERROR'])

    def test_rows_that_wait_for_a_retry_or_spent_every_attempt_are_not_probed(self):
        _waiting_row('outage', 40)
        for errors in (1, 3):
            failed = _attempt('outage', started_minutes_ago=30)
            TransitionMessage.objects.filter(pk=failed.pk).update(
                errors_count=errors,
                last_error_dt=timezone.now() - timedelta(minutes=29))
        crashed = _attempt('outage', started_minutes_ago=20)
        with patch.object(
            TransitionMessage, 'worker_holds_row', return_value=False,
        ) as probe:
            records = self._report()
        probe.assert_called_once_with(crashed.pk)
        reports = [r for r in records if "queue 'outage'" in r.getMessage()]
        self.assertEqual([r.levelname for r in reports], ['ERROR'])

    def test_each_queue_gets_its_own_line(self):
        _waiting_row('first', 30)
        _waiting_row('first', 20)
        _waiting_row('second', 30)
        records = self._report()
        messages = [record.getMessage() for record in records]
        self.assertEqual(len(messages), 2)
        self.assertIn("queue 'first'", messages[0])
        self.assertIn("queue 'second'", messages[1])

    def test_rows_inside_the_retry_window_are_not_reported(self):
        _waiting_row('q', 5)
        with self.assertNoLogs('django-logic', level='WARNING'):
            self.assertEqual(detect_stuck_transitions(), 0)
