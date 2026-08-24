"""The three periodic tasks that keep rows moving: retry, cleanup, detect stuck."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic.background import retry_pending
from django_logic.background.models import TransitionMessage
from django_logic.background.safety_nets import (
    cleanup_completed_transitions,
    detect_stuck_transitions,
    retry_pending,
)
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
        # sync_inventory is a BackgroundAction with no failed_state.
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
