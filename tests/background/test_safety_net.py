"""Periodic safety-net tasks: retry_stale, cleanup, detect_stuck."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic.background import retry_pending
from django_logic.background.models import TransitionMessage
from django_logic.background.tasks import (
    cleanup_completed_transitions,
    detect_stuck_transitions,
    _retry_pending_inline,
)
from tests.background.models import Widget


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 0,  # retry immediately
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


def _make_stale_tm(widget, transition_name='fulfil', errors=0, completed=False):
    tm = TransitionMessage.objects.create(
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
    # Back-date the created timestamp so RETRY_MINUTES filters include it.
    TransitionMessage.objects.filter(pk=tm.pk).update(
        created=timezone.now() - timedelta(minutes=5),
        modified=timezone.now() - timedelta(minutes=5),
    )
    tm.refresh_from_db()
    return tm


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class RetryStaleTests(TestCase):
    def test_picks_up_uncompleted_message(self):
        widget = Widget.objects.create(status='fulfilling')
        _make_stale_tm(widget)
        dispatched = _retry_pending_inline()
        self.assertEqual(dispatched, 1)
        widget.refresh_from_db()
        # Phase 2 ran inline — the widget moved to target state.
        self.assertEqual(widget.status, 'fulfilled')

    def test_skips_completed(self):
        widget = Widget.objects.create(status='fulfilled')
        _make_stale_tm(widget, completed=True)
        self.assertEqual(_retry_pending_inline(), 0)

    def test_stops_at_max_errors(self):
        widget = Widget.objects.create(status='fulfilling')
        _make_stale_tm(widget, errors=99)  # > MAX_ERRORS (3)
        self.assertEqual(_retry_pending_inline(), 0)

    def test_retry_pending_helper_is_public(self):
        widget = Widget.objects.create(status='fulfilling')
        _make_stale_tm(widget)
        self.assertEqual(retry_pending(), 1)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class CleanupTests(TestCase):
    def test_deletes_old_completed_messages(self):
        widget = Widget.objects.create()
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='fulfil',
            queue_name='q',
            is_completed=True,
        )
        TransitionMessage.objects.filter(pk=tm.pk).update(
            modified=timezone.now() - timedelta(days=30)
        )
        deleted = cleanup_completed_transitions()
        self.assertEqual(deleted, 1)

    def test_preserves_uncompleted_messages(self):
        widget = Widget.objects.create()
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='fulfil',
            queue_name='q',
            is_completed=False,
        )
        TransitionMessage.objects.filter(pk=tm.pk).update(
            modified=timezone.now() - timedelta(days=30)
        )
        self.assertEqual(cleanup_completed_transitions(), 0)
        self.assertTrue(TransitionMessage.objects.filter(pk=tm.pk).exists())


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class DetectStuckTests(TestCase):
    def test_finalizes_message_at_max_errors(self):
        """A row stuck at MAX_ERRORS is forcibly terminated: failed_state
        is written (since 'fulfil' declares one) and the TM is marked
        completed so the retry loop stops picking it up."""
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

        tm = TransitionMessage.objects.get(instance_id=widget.pk)
        self.assertTrue(tm.is_completed)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilment_failed')

    def test_no_failed_state_marks_completed_without_state_change(self):
        """If the transition has no failed_state declared, the row is
        marked completed but the model state is left at in_progress
        for operator review."""
        widget = Widget.objects.create(status='fulfilling')
        # Create a stuck TM for a transition that has no failed_state.
        # 'fulfil' has one; we need a different shape. Use a bespoke
        # throwaway transition — easier: just patch transition lookup
        # by picking an action_name that doesn't declare failed_state.
        # WidgetProcess.sync_inventory is a BackgroundAction without
        # failed_state.
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

        tm = TransitionMessage.objects.get(instance_id=widget.pk)
        self.assertTrue(tm.is_completed)
        widget.refresh_from_db()
        # Unchanged — sync_inventory is an Action with no failed_state.
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

    def test_unrestorable_row_still_marked_completed(self):
        """A stuck row pointing at a non-existent transition still gets
        terminated so the retry loop stops."""
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
        tm = TransitionMessage.objects.get(instance_id=widget.pk)
        self.assertTrue(tm.is_completed)
