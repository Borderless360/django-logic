"""The row-lock probe, and the two places that consult it.

``retry_status`` must not call a row stranded while a worker holds it, and
no claim may take such a row while it is held. Holding a
row lock from a second connection needs PostgreSQL; on SQLite the probe
always answers False, and the time-based classification stands — that
degradation is pinned here too.
"""
import threading
from datetime import timedelta
from unittest.mock import patch

from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic.background.models import TransitionMessage, in_flight
from django_logic.background.safety_nets import detect_stuck_transitions
from django_logic.testing import open_transition_message
from tests.background.models import Widget
from tests.stability.base import requires_postgres
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(
    TRANSITION_MESSAGE_MAX_ERRORS=3, TRANSITION_MESSAGE_RETRY_MINUTES=2,
)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class TimeBasedClassificationTests(TestCase):
    """Backend-independent: no second connection, so no lock is ever held."""

    def test_a_quiet_old_row_is_stranded(self):
        widget = Widget.objects.create(status='fulfilling')
        open_transition_message(
            widget, 'process', 'fulfil', started_minutes_ago=60,
        )
        self.assertEqual(
            TransitionMessage.retry_status(widget, 'process'),
            TransitionMessage.STRANDED,
        )

    def test_a_fresh_row_is_retrying(self):
        widget = Widget.objects.create(status='fulfilling')
        open_transition_message(widget, 'process', 'fulfil')
        self.assertEqual(
            TransitionMessage.retry_status(widget, 'process'),
            TransitionMessage.RETRYING,
        )

    def test_deferred_row_stays_retrying_until_eligibility_and_pickup_grace(self):
        from django_logic import conf

        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(widget, 'process', 'fulfil', started_minutes_ago=60)
        eligible_at = timezone.now() + timedelta(hours=1)
        TransitionMessage.objects.filter(pk=row.pk).update(eligible_at=eligible_at)
        window = timedelta(minutes=conf.retry_window_minutes())
        for now in (eligible_at - timedelta(minutes=1), eligible_at, eligible_at + window):
            with self.subTest(now=now), patch('django_logic.background.models.timezone.now', return_value=now):
                self.assertEqual(TransitionMessage.retry_status(widget, 'process'), TransitionMessage.RETRYING)
                self.assertTrue(in_flight(widget, 'process'))
        with patch('django_logic.background.models.timezone.now', return_value=eligible_at + window + timedelta(seconds=1)):
            self.assertEqual(TransitionMessage.retry_status(widget, 'process'), TransitionMessage.STRANDED)
            self.assertFalse(in_flight(widget, 'process'))

    def test_no_row_answers_none(self):
        widget = Widget.objects.create(status='draft')
        self.assertIsNone(TransitionMessage.retry_status(widget, 'process'))

    def test_probe_answers_false_without_a_holder(self):
        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(widget, 'process', 'fulfil')
        self.assertFalse(TransitionMessage.worker_holds_row(row.pk))


def _hold_row_lock(row_pk, locked, release):
    """Hold the row lock on a second connection until ``release`` is set."""
    try:
        with transaction.atomic():
            list(
                TransitionMessage.objects
                .select_for_update()
                .filter(pk=row_pk)
                .values_list('pk', flat=True)
            )
            locked.set()
            release.wait(timeout=30)
    finally:
        connections.close_all()


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
@requires_postgres
class HeldRowTests(TransactionTestCase):
    databases = '__all__'

    def _hold(self, row_pk):
        locked, release = threading.Event(), threading.Event()
        holder = threading.Thread(
            target=_hold_row_lock, args=(row_pk, locked, release),
        )
        holder.start()
        self.assertTrue(locked.wait(timeout=10))
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        return release

    def test_probe_answers_true_while_a_worker_holds_the_row(self):
        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(widget, 'process', 'fulfil')
        self._hold(row.pk)
        self.assertTrue(TransitionMessage.worker_holds_row(row.pk))

    def test_a_held_long_attempt_keeps_its_queue_served(self):
        waiting = open_transition_message(
            Widget.objects.create(status='fulfilling'), 'process', 'fulfil',
            queue_name='slow',
        )
        TransitionMessage.objects.filter(pk=waiting.pk).update(
            created=timezone.now() - timedelta(minutes=30))
        attempt = open_transition_message(
            Widget.objects.create(status='fulfilling'), 'process', 'fulfil',
            queue_name='slow', started_minutes_ago=25,
        )
        self._hold(attempt.pk)
        with self.assertLogs('django-logic', level='WARNING') as logs:
            detect_stuck_transitions()
        self.assertEqual(
            [record.levelname for record in logs.records], ['WARNING'])

    def test_a_held_old_row_is_retrying_not_stranded(self):
        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(
            widget, 'process', 'fulfil', started_minutes_ago=60,
        )
        release = self._hold(row.pk)
        self.assertEqual(
            TransitionMessage.retry_status(widget, 'process'),
            TransitionMessage.RETRYING,
        )
        release.set()

    def test_the_retry_pass_leaves_a_held_row_alone(self):
        from django_logic.background.safety_nets import retry_pending

        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(
            widget, 'process', 'fulfil', started_minutes_ago=60,
        )
        release = self._hold(row.pk)
        # The attempt's row lock is the guard: the pass visits the row,
        # the runner gives it up, and nothing about it changes.
        retry_pending()
        row.refresh_from_db()
        widget.refresh_from_db()
        self.assertFalse(row.is_completed)
        self.assertEqual(widget.status, 'fulfilling')
        release.set()
