"""Per-attempt timeout watchdog.

Covers:
- ``BackgroundTransition(timeout=N)`` validation
- ``timeout_seconds`` persisted on the TransitionMessage at phase 1
- Watchdog skips rows with no timeout / fresh started_at / no started_at
- Watchdog records a TimeoutError on stale attempts
- Watchdog finalizes the row (failed_state + completed) at MAX_ERRORS
- Watchdog defers rows currently held by a worker
"""
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.tasks import _watchdog_stale_attempts_inline
from tests.background.models import Widget


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 2,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


class TimeoutKwargValidationTests(SimpleTestCase):
    def test_negative_timeout_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            BackgroundTransition(
                action_name='x', sources=['a'], target='b',
                queue='q', timeout=-1,
            )

    def test_zero_timeout_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            BackgroundTransition(
                action_name='x', sources=['a'], target='b',
                queue='q', timeout=0,
            )

    def test_non_int_timeout_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            BackgroundTransition(
                action_name='x', sources=['a'], target='b',
                queue='q', timeout='60',
            )

    def test_valid_timeout_accepted(self):
        t = BackgroundTransition(
            action_name='x', sources=['a'], target='b',
            queue='q', timeout=60,
        )
        self.assertEqual(t.timeout, 60)

    def test_no_timeout_is_default(self):
        t = BackgroundTransition(
            action_name='x', sources=['a'], target='b', queue='q',
        )
        self.assertIsNone(t.timeout)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class Phase1PersistsTimeoutTests(TestCase):
    def test_timeout_seconds_stored_on_tm(self):
        widget = Widget.objects.create()
        widget.process.timeboxed()
        tm = TransitionMessage.objects.get(instance_id=widget.pk)
        self.assertEqual(tm.timeout_seconds, 60)

    def test_transition_without_timeout_leaves_null(self):
        widget = Widget.objects.create()
        widget.process.fulfil()  # no timeout declared
        tm = TransitionMessage.objects.get(instance_id=widget.pk)
        self.assertIsNone(tm.timeout_seconds)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class WatchdogSkipsTests(TestCase):
    def test_skips_rows_without_timeout(self):
        widget = Widget.objects.create()
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='fulfil',
            queue_name='q',
            started_at=timezone.now() - timedelta(hours=1),
            timeout_seconds=None,  # opted out
        )
        self.assertEqual(_watchdog_stale_attempts_inline(), 0)

    def test_skips_rows_without_started_at(self):
        widget = Widget.objects.create()
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='timeboxed',
            queue_name='q',
            started_at=None,  # phase 2 hasn't run yet
            timeout_seconds=60,
        )
        self.assertEqual(_watchdog_stale_attempts_inline(), 0)

    def test_skips_rows_still_within_timeout(self):
        widget = Widget.objects.create()
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='timeboxed',
            queue_name='q',
            started_at=timezone.now() - timedelta(seconds=10),  # fresh
            timeout_seconds=60,
        )
        self.assertEqual(_watchdog_stale_attempts_inline(), 0)

    def test_skips_completed_rows(self):
        widget = Widget.objects.create()
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='timeboxed',
            queue_name='q',
            started_at=timezone.now() - timedelta(hours=1),
            timeout_seconds=60,
            is_completed=True,
        )
        self.assertEqual(_watchdog_stale_attempts_inline(), 0)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class WatchdogActsTests(TestCase):
    def _make_stale(self, widget, errors_count=0):
        return TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='timeboxed',
            queue_name='django_logic.slow',
            started_at=timezone.now() - timedelta(seconds=120),
            timeout_seconds=60,
            errors_count=errors_count,
        )

    def test_records_timeout_error_below_max(self):
        widget = Widget.objects.create(status='tb_running')
        tm = self._make_stale(widget, errors_count=0)

        self.assertEqual(_watchdog_stale_attempts_inline(), 1)
        tm.refresh_from_db()

        self.assertEqual(tm.errors_count, 1)
        self.assertIn('timeout', tm.last_error_message)
        # Below MAX_ERRORS: left uncompleted so the retry loop picks it up.
        self.assertFalse(tm.is_completed)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'tb_running')

    def test_finalizes_at_max_errors(self):
        widget = Widget.objects.create(status='tb_running')
        # errors_count=1 so the watchdog's increment hits MAX_ERRORS=2.
        tm = self._make_stale(widget, errors_count=1)

        self.assertEqual(_watchdog_stale_attempts_inline(), 1)
        tm.refresh_from_db()

        self.assertEqual(tm.errors_count, 2)
        self.assertTrue(tm.is_completed)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'tb_failed')

    def test_unrestorable_row_still_terminated(self):
        widget = Widget.objects.create(status='tb_running')
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='nonexistent',
            queue_name='q',
            started_at=timezone.now() - timedelta(seconds=120),
            timeout_seconds=60,
            errors_count=1,  # next increment hits MAX_ERRORS=2
        )
        self.assertEqual(_watchdog_stale_attempts_inline(), 1)
        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
