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
        self.assertTrue(run_once(_CRITICAL, isolate=False))
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

    def test_a_crashing_attempt_is_contained_and_counted(self):
        import tempfile
        widget = Widget.objects.create(status='draft')
        marker = tempfile.mktemp(prefix='dl_die_once_')
        widget.process.die_once(marker_path=marker)
        # First attempt: the child process hard-kills itself. The worker
        # survives, and the death is an error on the row.
        self.assertTrue(run_once(_CRITICAL, isolate=True))
        row = TransitionMessage.objects.get()
        self.assertFalse(row.is_completed)
        self.assertEqual(row.errors_count, 1)
        self.assertIn('[crashed]', row.last_error_message)
        # The recorded error paces the retry: not claimable inside the wait.
        self.assertIsNone(claim_next(_CRITICAL))
        with override_settings(DJANGO_LOGIC=dl_settings(
            BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_RETRY_MINUTES=0,
        )):
            self.assertTrue(run_once(_CRITICAL, isolate=True))
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'survived')

    def test_a_death_is_not_recorded_on_a_row_another_worker_completed(self):
        from django_logic.background.pull import _record_child_death

        widget = Widget.objects.create(status='fulfilled')
        row = open_transition_message(widget, 'process', 'fulfil')
        TransitionMessage.objects.filter(pk=row.pk).update(is_completed=True)
        _record_child_death(row.pk, 1)
        row.refresh_from_db()
        self.assertEqual(row.errors_count, 0)
        self.assertEqual(row.last_error_message, '')

    def test_the_safety_nets_run_during_a_sustained_backlog(self):
        from unittest.mock import patch

        first = Widget.objects.create(status='draft')
        second = Widget.objects.create(status='draft')
        first.process.fulfil()
        second.process.fulfil()
        ran = []
        with patch('django_logic.background.pull.SAFETY_NET_SECONDS', 0), \
                patch('django_logic.background.pull._run_safety_nets',
                      side_effect=lambda: ran.append(1)):
            run_worker(_CRITICAL, forever=False)
        # Nets due after every claim: they ran at least once per drained row,
        # not only after the backlog emptied.
        self.assertGreaterEqual(len(ran), 2)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, 'fulfilled')
        self.assertEqual(second.status, 'fulfilled')

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


class WaitDegradeTests(TestCase):
    """When the connection cannot listen, the wait sleeps the poll
    interval instead of raising — the poll floor carries the loop."""

    def test_wait_degrades_to_a_plain_sleep(self):
        from unittest.mock import MagicMock, patch

        from django_logic.background.pull import _wait_for_work

        fake_connections = MagicMock()
        fake_connections.__getitem__.side_effect = RuntimeError('no connection')
        with patch('django_logic.background.pull.connections', fake_connections), \
                patch('django_logic.background.pull.time.sleep') as fake_sleep:
            _wait_for_work(3.5)
        fake_sleep.assert_called_once_with(3.5)


@override_settings(DJANGO_LOGIC=_PULL_SETTINGS)
@requires_postgres
class NotificationWakeUpTests(TransactionTestCase):
    """A committed NOTIFY wakes a waiting worker before the poll floor."""

    databases = '__all__'

    def test_a_notification_wakes_the_wait_before_the_timeout(self):
        import time

        from django_logic.background.pull import _wait_for_work, notify_workers

        # The first wait issues LISTEN for the session.
        _wait_for_work(0.01)

        def notify_from_another_connection():
            try:
                time.sleep(0.3)
                notify_workers()
            finally:
                connections.close_all()

        notifier = threading.Thread(target=notify_from_another_connection)
        started = time.monotonic()
        notifier.start()
        try:
            _wait_for_work(10.0)
        finally:
            notifier.join()
        waited = time.monotonic() - started
        # A wait that ignores the notification takes the whole timeout.
        self.assertLess(waited, 5.0)
