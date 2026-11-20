"""The cleanup sweep keeps the one row an investigation needs.

Old completed rows are deleted, except the newest terminal-failure row per
instance and process — the only explanation for an instance parked in its
``failed_state``.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import UNRESTORABLE_MARKER
from django_logic.background.safety_nets import cleanup_completed_transitions
from tests.background.models import Widget
from tests import dl_settings


_SETTINGS = dl_settings(
    TRANSITION_MESSAGE_MAX_ERRORS=3, TRANSITION_MESSAGE_CLEANUP_DAYS=7,
)


def _old_completed_row(widget, *, errors=0, last_error='', days_ago=30,
                       process_name='process', transition_name='fulfil',
                       failed=False):
    row = TransitionMessage.objects.create(
        app_label='bg_tests',
        model_name='widget',
        instance_id=str(widget.pk),
        process_name=process_name,
        transition_name=transition_name,
        queue_name='django_logic.critical',
        kwargs={},
        errors_count=errors,
        last_error_message=last_error,
        is_completed=True,
        ended_in_failure=failed,
    )
    past = timezone.now() - timedelta(days=days_ago)
    TransitionMessage.objects.filter(pk=row.pk).update(
        created=past, modified=past, completed_at=past,
    )
    return row


@override_settings(DJANGO_LOGIC=_SETTINGS)
class CleanupRetentionTests(TestCase):
    def test_old_successes_are_deleted(self):
        widget = Widget.objects.create(status='fulfilled')
        _old_completed_row(widget)
        self.assertEqual(cleanup_completed_transitions(), 1)
        self.assertEqual(TransitionMessage.objects.count(), 0)

    def test_the_newest_terminal_failure_row_stays(self):
        widget = Widget.objects.create(status='fulfilment_failed')
        _old_completed_row(
            widget, errors=3, last_error='boom', days_ago=40, failed=True,
        )
        newest = _old_completed_row(
            widget, errors=3, last_error='boom again', days_ago=20, failed=True,
        )
        _old_completed_row(widget, days_ago=30)  # a success in between
        deleted = cleanup_completed_transitions()
        self.assertEqual(deleted, 2)
        kept = TransitionMessage.objects.get()
        self.assertEqual(kept.pk, newest.pk)
        self.assertEqual(kept.last_error_message, 'boom again')

    def test_an_unrestorable_row_counts_as_a_failure(self):
        widget = Widget.objects.create(status='fulfilling')
        row = _old_completed_row(
            widget, errors=0,
            last_error=f'{UNRESTORABLE_MARKER} model gone', failed=True,
        )
        cleanup_completed_transitions()
        self.assertTrue(TransitionMessage.objects.filter(pk=row.pk).exists())

    def test_a_permanent_failure_row_is_kept(self):
        widget = Widget.objects.create(status='draft')
        from django_logic.background import PermanentFailure
        with self.assertRaises(PermanentFailure):
            widget.process.refuse()
        row = TransitionMessage.objects.get(transition_name='refuse')
        self.assertTrue(row.ended_in_failure)
        self.assertEqual(row.errors_count, 1)
        past = timezone.now() - timedelta(days=30)
        TransitionMessage.objects.filter(pk=row.pk).update(
            modified=past, completed_at=past,
        )
        self.assertEqual(cleanup_completed_transitions(), 0)
        self.assertTrue(TransitionMessage.objects.filter(pk=row.pk).exists())

    def test_retention_is_per_instance_and_process(self):
        widget = Widget.objects.create(status='fulfilment_failed')
        other = Widget.objects.create(status='audit_failed')
        keep_one = _old_completed_row(
            widget, errors=3, last_error='a', failed=True,
        )
        keep_two = _old_completed_row(
            other, errors=3, last_error='b', process_name='audit_process',
            transition_name='audit', failed=True,
        )
        cleanup_completed_transitions()
        self.assertEqual(
            set(TransitionMessage.objects.values_list('pk', flat=True)),
            {keep_one.pk, keep_two.pk},
        )

    def test_fresh_completed_rows_are_untouched(self):
        widget = Widget.objects.create(status='fulfilled')
        _old_completed_row(widget, days_ago=1)
        self.assertEqual(cleanup_completed_transitions(), 0)
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_completion_time_and_ties_determine_the_retained_failure_across_batches(self):
        for same_completion_time in (False, True):
            with self.subTest(same_completion_time=same_completion_time):
                widget = Widget.objects.create(status='fulfilled')
                first = _old_completed_row(widget, days_ago=20, failed=True)
                second = _old_completed_row(widget, days_ago=30, failed=True)
                completed = timezone.now() - timedelta(days=20)
                TransitionMessage.objects.filter(pk=first.pk).update(
                    completed_at=completed, modified=completed - timedelta(days=20),
                )
                TransitionMessage.objects.filter(pk=second.pk).update(
                    completed_at=completed if same_completion_time else completed - timedelta(days=10),
                    modified=completed + timedelta(days=10),
                )
                _old_completed_row(widget, days_ago=8)  # Later successful work also expires.

                with patch('django_logic.background.safety_nets._CLEANUP_BATCH_SIZE', 1):
                    self.assertEqual(cleanup_completed_transitions(), 2)

                expected = second if same_completion_time else first
                self.assertEqual(
                    list(TransitionMessage.objects.filter(instance_id=str(widget.pk)).values_list('pk', flat=True)),
                    [expected.pk],
                )

    def test_a_recent_failure_replaces_the_older_failure_kept_for_the_same_process(self):
        widget = Widget.objects.create(status='fulfilment_failed')
        _old_completed_row(widget, days_ago=30, failed=True)
        recent = _old_completed_row(widget, days_ago=1, failed=True)

        self.assertEqual(cleanup_completed_transitions(), 1)

        self.assertEqual(list(TransitionMessage.objects.values_list('pk', flat=True)), [recent.pk])
