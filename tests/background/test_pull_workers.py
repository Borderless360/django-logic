"""Pull execution: the claim is the retry rule, the row lock is the guard.

The claim needs real row locks (``SKIP LOCKED``), so most of these run on
PostgreSQL only — the same gating as the other concurrency tests. The
one SQLite-safe piece is the enqueue contract: pull mode leaves the
committed row for a worker instead of running inline.
"""
import threading
from datetime import timedelta

from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_logic.background.models import TransitionMessage
from django_logic.background.pull import claim_next, run_once, run_worker
from django_logic.testing import open_transition_message
from tests.background.models import Widget
from tests.stability.base import requires_postgres
from tests import dl_settings


_PULL_SETTINGS = dl_settings(
    BACKGROUND_EXECUTION='pull',
    TRANSITION_MESSAGE_MAX_ERRORS=3,
    TRANSITION_MESSAGE_RETRY_MINUTES=2,
)

_CRITICAL = ['django_logic.critical']


@override_settings(DJANGO_LOGIC=_PULL_SETTINGS)
class PullEnqueueTests(TestCase):
    def test_enqueue_leaves_the_row_for_a_worker(self):
        widget = Widget.objects.create(status='draft')
        with self.captureOnCommitCallbacks(execute=True):
            widget.process.fulfil()
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilling')
        row = TransitionMessage.objects.get()
        self.assertFalse(row.is_completed)

    def test_the_starter_has_nothing_to_do(self):
        from django_logic.background.tasks import _retry_pending_inline

        widget = Widget.objects.create(status='fulfilling')
        open_transition_message(
            widget, 'process', 'fulfil', started_minutes_ago=60,
        )
        self.assertEqual(_retry_pending_inline(), 0)


def _hold_row_lock(row_pk, locked, release):
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


@override_settings(DJANGO_LOGIC=_PULL_SETTINGS)
@requires_postgres
class PullClaimTests(TransactionTestCase):
    databases = '__all__'

    def _row(self, status='fulfilling', queue='django_logic.critical'):
        widget = Widget.objects.create(status=status)
        return widget, open_transition_message(
            widget, 'process', 'fulfil', queue_name=queue,
        )

    def test_a_fresh_row_is_claimed_and_completed(self):
        # TransactionTestCase commits for real, so the NOTIFY hook fires
        # on its own; no capture is needed.
        widget = Widget.objects.create(status='draft')
        widget.process.fulfil()
        self.assertTrue(run_once(_CRITICAL))
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilled')
        self.assertTrue(TransitionMessage.objects.get().is_completed)

    def test_a_failed_row_waits_out_the_retry_window(self):
        _, row = self._row()
        now = timezone.now()
        TransitionMessage.objects.filter(pk=row.pk).update(
            errors_count=1, last_error_dt=now,
        )
        self.assertIsNone(claim_next(_CRITICAL))
        TransitionMessage.objects.filter(pk=row.pk).update(
            last_error_dt=now - timedelta(minutes=3),
        )
        self.assertEqual(claim_next(_CRITICAL), row.pk)

    def test_a_running_attempt_is_skipped(self):
        _, row = self._row()
        locked, release = threading.Event(), threading.Event()
        holder = threading.Thread(
            target=_hold_row_lock, args=(row.pk, locked, release),
        )
        holder.start()
        self.assertTrue(locked.wait(timeout=10))
        self.addCleanup(holder.join)
        try:
            self.assertIsNone(claim_next(_CRITICAL))
        finally:
            release.set()
            holder.join()
        self.assertEqual(claim_next(_CRITICAL), row.pk)

    def test_the_queue_filter_holds(self):
        _, row = self._row(queue='django_logic.slow')
        self.assertIsNone(claim_next(_CRITICAL))
        self.assertEqual(claim_next(['django_logic.slow']), row.pk)

    def test_an_exhausted_row_is_not_claimed(self):
        _, row = self._row()
        TransitionMessage.objects.filter(pk=row.pk).update(errors_count=3)
        self.assertIsNone(claim_next(_CRITICAL))

    def test_one_loop_pass_drains_and_runs_the_safety_nets(self):
        first = Widget.objects.create(status='draft')
        second = Widget.objects.create(status='draft')
        first.process.fulfil()
        second.process.fulfil()
        run_worker(_CRITICAL, forever=False)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, 'fulfilled')
        self.assertEqual(second.status, 'fulfilled')
        self.assertFalse(
            TransitionMessage.objects.filter(is_completed=False).exists()
        )
