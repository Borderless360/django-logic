"""Safety-net reports and scans continue when one row cannot finish."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.test import TestCase, override_settings

from django_logic.background import safety_nets
from django_logic.background.models import TransitionMessage
from tests import dl_settings
from tests.background.models import Widget


_SETTINGS = dl_settings(
    TRANSITION_MESSAGE_MAX_ERRORS=3,
    TRANSITION_MESSAGE_RETRY_MINUTES=4,
)
_NOW = datetime(2026, 9, 28, 12, tzinfo=datetime_timezone.utc)


def make_message(*, created, queue='reporting', status='fulfilling',
                 transition_name='fulfil', **fields):
    widget = Widget.objects.create(status=status)
    message = TransitionMessage.objects.create(
        app_label='bg_tests', model_name='widget', instance_id=str(widget.pk),
        process_name='process', transition_name=transition_name,
        queue_name=queue, **fields,
    )
    TransitionMessage.objects.filter(pk=message.pk).update(created=created)
    message.refresh_from_db()
    return widget, message


@override_settings(DJANGO_LOGIC=_SETTINGS, USE_TZ=True)
class SafetyNetReportingTests(TestCase):
    def test_only_old_unstarted_rows_are_reported_without_changes(self):
        cutoff = _NOW - timedelta(minutes=16)
        _, overdue = make_message(created=cutoff - timedelta(microseconds=1), queue='unserved')
        make_message(created=cutoff)
        make_message(created=cutoff + timedelta(microseconds=1))
        make_message(created=cutoff - timedelta(minutes=1), started_at=cutoff)
        make_message(created=cutoff - timedelta(minutes=1), is_completed=True)
        before = list(TransitionMessage.objects.order_by('pk').values())

        with patch.object(safety_nets.timezone, 'now', return_value=_NOW), \
                self.assertLogs('django-logic', level='ERROR') as captured:
            self.assertEqual(safety_nets.detect_stuck_transitions(), 0)

        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelname, 'ERROR')
        report = captured.records[0].getMessage()
        self.assertIn(f'TransitionMessage#{overdue.pk}', report)
        self.assertIn("queue 'unserved'", report)
        self.assertIn('dl_worker --queues unserved', report)
        self.assertEqual(list(TransitionMessage.objects.order_by('pk').values()), before)

    def test_a_failed_finalizer_does_not_stop_other_rows_or_inflate_the_count(self):
        widgets = {}
        for _ in range(3):
            widget, message = make_message(created=_NOW, errors_count=3)
            widgets[message.pk] = widget
        attempted = []
        original_finalize = safety_nets.finalize_stuck_attempt

        def finalize_one(message_id):
            attempted.append(message_id)
            if len(attempted) == 1:
                raise RuntimeError('The first row cannot finish.')
            if len(attempted) == 2:
                return False
            return original_finalize(message_id)

        with patch.object(safety_nets, 'finalize_stuck_attempt', side_effect=finalize_one), \
                patch.object(safety_nets.timezone, 'now', return_value=_NOW), \
                self.assertLogs('django-logic', level='ERROR') as captured:
            self.assertEqual(safety_nets.detect_stuck_transitions(), 1)

        self.assertCountEqual(attempted, widgets)
        self.assertEqual(len(captured.records), 1)
        self.assertIn(f'TransitionMessage#{attempted[0]}', captured.records[0].getMessage())
        self.assertIn('first row cannot finish', captured.records[0].getMessage())
        for message_id in attempted[:2]:
            message = TransitionMessage.objects.get(pk=message_id)
            widgets[message_id].refresh_from_db()
            self.assertFalse(message.is_completed)
            self.assertEqual(message.errors_count, 3)
            self.assertEqual(widgets[message_id].status, 'fulfilling')
            self.assertEqual(widgets[message_id].cb_log, '')
        completed = TransitionMessage.objects.get(pk=attempted[2])
        finished_widget = widgets[completed.pk]
        finished_widget.refresh_from_db()
        self.assertTrue(completed.is_completed)
        self.assertEqual(finished_widget.status, 'fulfilment_failed')
        self.assertEqual(finished_widget.cb_log, 'fcb,')

    def test_a_real_failed_retry_does_not_stop_the_next_row(self):
        failed_widget, failed_message = make_message(
            created=_NOW - timedelta(minutes=2),
            status='crashing', transition_name='crash',
        )
        completed_widget, completed_message = make_message(created=_NOW - timedelta(minutes=1))

        with patch.object(safety_nets.timezone, 'now', return_value=_NOW), \
                self.assertLogs('django-logic', level='WARNING') as captured:
            self.assertEqual(safety_nets.retry_pending(), 1)

        failed_widget.refresh_from_db()
        failed_message.refresh_from_db()
        completed_widget.refresh_from_db()
        completed_message.refresh_from_db()
        self.assertEqual(failed_message.errors_count, 1)
        self.assertFalse(failed_message.is_completed)
        self.assertIn('boom', failed_message.last_error_message)
        self.assertEqual(failed_widget.status, 'crashing')
        self.assertEqual(failed_widget.se_log, '')
        self.assertTrue(completed_message.is_completed)
        self.assertEqual(completed_message.errors_count, 0)
        self.assertEqual(completed_widget.status, 'fulfilled')
        self.assertEqual(completed_widget.se_log, 'ok,')
        self.assertEqual(completed_widget.cb_log, 'cb,')
        warnings = [record for record in captured.records if record.levelname == 'WARNING']
        self.assertEqual(len(warnings), 1)
        self.assertIn(f'retry_pending: TransitionMessage#{failed_message.pk}', warnings[0].getMessage())


@override_settings(DJANGO_LOGIC=_SETTINGS, USE_TZ=True)
class ClaimableOrderingTests(TestCase):
    def test_queue_filters_preserve_oldest_first_order_and_retry_boundaries(self):
        cutoff = _NOW - timedelta(minutes=4)
        _, newest = make_message(created=_NOW - timedelta(minutes=3), queue='fast')
        _, oldest = make_message(
            created=_NOW - timedelta(minutes=7), queue='slow', errors_count=2,
            last_error_dt=cutoff - timedelta(microseconds=1),
        )
        _, middle = make_message(
            created=_NOW - timedelta(minutes=5), queue='fast', errors_count=1,
            last_error_dt=cutoff - timedelta(microseconds=1),
        )
        make_message(created=_NOW - timedelta(minutes=6), errors_count=1, last_error_dt=cutoff)
        make_message(created=_NOW - timedelta(minutes=8), errors_count=1,
                     last_error_dt=cutoff + timedelta(microseconds=1))
        make_message(created=_NOW - timedelta(minutes=9), errors_count=3,
                     last_error_dt=cutoff - timedelta(minutes=1))
        make_message(created=_NOW - timedelta(minutes=10), errors_count=4)
        make_message(created=_NOW - timedelta(minutes=11), is_completed=True)

        with patch.object(safety_nets.timezone, 'now', return_value=_NOW):
            for queues, expected in [
                (None, [oldest.pk, middle.pk, newest.pk]),
                (['fast'], [middle.pk, newest.pk]),
                (['slow'], [oldest.pk]),
                (['fast', 'slow'], [oldest.pk, middle.pk, newest.pk]),
                ([], []),
            ]:
                with self.subTest(queues=queues):
                    self.assertEqual(
                        list(safety_nets._claimable(queues).values_list('pk', flat=True)), expected,
                    )
