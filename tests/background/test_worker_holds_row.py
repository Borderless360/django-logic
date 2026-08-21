"""The row-lock probe, and the two places that consult it.

``retry_status`` must not call a row stranded while a worker holds it, and
the periodic starter must not send such a row to the queue again. Holding a
row lock from a second connection needs PostgreSQL; on SQLite the probe
always answers False, and the time-based classification stands — that
degradation is pinned here too.
"""
import threading

from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from django_logic.background.models import TransitionMessage
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

    def test_starter_skips_a_row_a_worker_holds(self):
        from django_logic.background.dispatch import sync_execution
        from django_logic.background.tasks import _retry_pending_inline

        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(
            widget, 'process', 'fulfil', started_minutes_ago=60,
        )
        release = self._hold(row.pk)
        # Celery mode would apply_async; the probe must skip the held row
        # before any dispatch. Settings stay on celery mode here — the row
        # is skipped, so nothing is ever sent.
        with override_settings(
            DJANGO_LOGIC=dl_settings(
                BACKGROUND_EXECUTION='celery',
                TRANSITION_MESSAGE_RETRY_MINUTES=2,
            ),
        ):
            self.assertEqual(_retry_pending_inline(), 0)
        release.set()
