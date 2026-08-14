"""The per-attempt timeout watchdog: ``BackgroundTransition(timeout=N)``
validation, the ``timeout_seconds`` column the enqueue writes, and what the
watchdog does with a stale attempt.
"""
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.db.models.signals import post_init
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.tasks import _watchdog_stale_attempts_inline
from tests.background.models import Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=2)


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
class EnqueuePersistsTimeoutTests(TestCase):
    def test_timeout_seconds_stored_on_the_row(self):
        widget = Widget.objects.create()
        widget.process.timeboxed()
        transition_message = TransitionMessage.objects.get(
            instance_id=widget.pk)
        self.assertEqual(transition_message.timeout_seconds, 60)

    def test_transition_without_timeout_leaves_null(self):
        widget = Widget.objects.create()
        widget.process.fulfil()  # no timeout declared
        transition_message = TransitionMessage.objects.get(
            instance_id=widget.pk)
        self.assertIsNone(transition_message.timeout_seconds)


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
            started_at=None,  # no worker has started it yet
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
        transition_message = self._make_stale(widget, errors_count=0)

        self.assertEqual(_watchdog_stale_attempts_inline(), 1)
        transition_message.refresh_from_db()

        self.assertEqual(transition_message.errors_count, 1)
        self.assertIn('timeout', transition_message.last_error_message)
        # Below MAX_ERRORS: left uncompleted so the retry loop picks it up.
        self.assertFalse(transition_message.is_completed)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'tb_running')

    def test_finalizes_at_max_errors(self):
        widget = Widget.objects.create(status='tb_running')
        # errors_count=1 so the watchdog's increment hits MAX_ERRORS=2.
        transition_message = self._make_stale(widget, errors_count=1)

        self.assertEqual(_watchdog_stale_attempts_inline(), 1)
        transition_message.refresh_from_db()

        self.assertEqual(transition_message.errors_count, 2)
        self.assertTrue(transition_message.is_completed)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'tb_failed')

    def test_unrestorable_row_still_terminated(self):
        widget = Widget.objects.create(status='tb_running')
        transition_message = TransitionMessage.objects.create(
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
        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)


def _break_the_failed_state_write(sender, instance, **kwargs):
    """Make the watchdog finalizer's ``failed_state`` savepoint roll back with
    no visible error.

    ``post_init`` fires from the ``refresh_from_db`` that ends ``set_state``,
    so the suppressed database error is the last statement in the savepoint and
    nothing after it reports the rollback.
    """
    if instance.pk is None or instance.status != 'tb_failed':
        return
    try:
        Widget.objects.create(pk=instance.pk)
    except IntegrityError:
        pass


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class WatchdogFinalizeWriteDiscardedTests(TestCase):
    """When the watchdog finalizer's ``failed_state`` write is rolled back, it
    must record the failure instead of logging that the state changed."""

    def test_a_discarded_write_is_reported_not_logged_as_written(self):
        widget = Widget.objects.create(status='tb_running')
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='timeboxed',
            queue_name='django_logic.slow',
            started_at=timezone.now() - timedelta(seconds=120),
            timeout_seconds=60,
            errors_count=1,  # next increment hits MAX_ERRORS=2
        )
        post_init.connect(_break_the_failed_state_write, sender=Widget)
        self.addCleanup(
            post_init.disconnect, _break_the_failed_state_write, sender=Widget)

        with self.assertLogs('django-logic.transition', level='INFO') as logs:
            self.assertEqual(_watchdog_stale_attempts_inline(), 1)

        transition_message.refresh_from_db()
        widget.refresh_from_db()
        # The row still completes, which is what stops the retry loop.
        self.assertTrue(transition_message.is_completed)
        # The write was discarded, so the instance keeps its old state.
        self.assertEqual(widget.status, 'tb_running')
        self.assertIn(
            'failed_state write',
            transition_message.failure_side_effect_error)
        output = '\n'.join(logs.output)
        self.assertIn('could not write failed_state', output)
        self.assertNotIn('set failed_state=', output)
