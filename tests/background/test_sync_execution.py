"""End-to-end: BackgroundTransition + BackgroundAction under Sync mode.

Sync mode is what the test suite runs by default (see tests/settings.py),
so calling ``instance.process.fulfil()`` executes phase 1 **and** phase 2
inline and we can assert on the resulting state directly.
"""
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from django_logic.background import sync_execution
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from tests.background.models import Widget


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class HappyPathTests(TestCase):
    def setUp(self):
        self.widget = Widget.objects.create()

    def test_transition_reaches_target(self):
        tr_id = self.widget.process.fulfil()
        self.assertIsNotNone(tr_id)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilled')
        self.assertIn('ok,', self.widget.se_log)
        self.assertIn('cb,', self.widget.cb_log)
        self.assertNotIn('fcb,', self.widget.cb_log)

    def test_transition_message_is_marked_completed(self):
        self.widget.process.fulfil()
        tm = TransitionMessage.objects.get(
            app_label='bg_tests',
            model_name='widget',
            instance_id=self.widget.pk,
        )
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.errors_count, 0)
        self.assertEqual(tm.queue_name, 'django_logic.critical')

    def test_queue_name_persisted(self):
        self.widget.status = 'fulfilled'
        self.widget.save()
        self.widget.process.generate_export()
        tm = TransitionMessage.objects.get(transition_name='generate_export')
        self.assertEqual(tm.queue_name, 'django_logic.slow')

    def test_chained_transitions(self):
        self.widget.process.fulfil()
        self.widget.refresh_from_db()
        self.widget.process.generate_export()
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'exported')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class BackgroundActionTests(TestCase):
    def setUp(self):
        self.widget = Widget.objects.create(status='fulfilled')

    def test_action_runs_without_state_change(self):
        self.widget.process.sync_inventory()
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilled')  # unchanged
        self.assertIn('ok,', self.widget.se_log)

    def test_action_records_transition_message(self):
        self.widget.process.sync_inventory()
        tm = TransitionMessage.objects.get(transition_name='sync_inventory')
        self.assertTrue(tm.is_completed)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class FailurePathTests(TestCase):
    def setUp(self):
        self.widget = Widget.objects.create()

    def test_exception_propagates_in_sync_mode(self):
        with self.assertRaises(ValueError) as ctx:
            self.widget.process.crash()
        self.assertEqual(str(ctx.exception), 'boom')

    def test_errors_count_incremented_below_max(self):
        with self.assertRaises(ValueError):
            self.widget.process.crash()
        tm = TransitionMessage.objects.get(transition_name='crash')
        self.assertFalse(tm.is_completed)
        self.assertEqual(tm.errors_count, 1)
        self.assertEqual(tm.last_error_message, 'boom')
        # State stays in in_progress_state because retry is still pending.
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'crashing')

    def test_reaches_max_errors_and_writes_failed_state(self):
        # Raise the budget of errors to 1 so we hit terminal on first shot.
        with override_settings(
            DJANGO_LOGIC=dict(_SYNC_SETTINGS, TRANSITION_MESSAGE_MAX_ERRORS=1)
        ):
            with self.assertRaises(ValueError):
                self.widget.process.crash()
        tm = TransitionMessage.objects.get(transition_name='crash')
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.errors_count, 1)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'crash_failed')
        self.assertIn('fcb,', self.widget.cb_log)

    def test_background_action_failure_writes_failed_state(self):
        self.widget.status = 'fulfilled'
        self.widget.save()
        with override_settings(
            DJANGO_LOGIC=dict(_SYNC_SETTINGS, TRANSITION_MESSAGE_MAX_ERRORS=1)
        ):
            with self.assertRaises(ValueError):
                self.widget.process.crash_action()
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'sync_failed')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class ConcurrencyTests(TestCase):
    def test_second_concurrent_request_rejected(self):
        widget = Widget.objects.create()
        # Simulate: the first phase 1 committed (TM exists, state=fulfilling)
        # but phase 2 hasn't completed.
        TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=widget.pk,
            process_name='process',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={},
        )
        widget.status = 'fulfilling'
        widget.save()

        fresh = Widget.objects.get(pk=widget.pk)
        fresh.status = 'draft'  # pretend the caller still sees draft
        with self.assertRaises(AlreadyInProgress):
            # Bypass the "not in sources" check by forcing the source.
            fresh.status = 'draft'
            fresh.save()
            fresh.process.fulfil()


class SyncExecutionContextManagerTests(TestCase):
    """sync_execution() should force Sync mode even if the global is 'celery'."""

    def test_context_manager_overrides_setting(self):
        celery_cfg = dict(_SYNC_SETTINGS, BACKGROUND_EXECUTION='celery')
        with override_settings(DJANGO_LOGIC=celery_cfg):
            widget = Widget.objects.create()
            with sync_execution():
                widget.process.fulfil()
            widget.refresh_from_db()
            self.assertEqual(widget.status, 'fulfilled')


class CeleryModeWithoutCeleryTests(TestCase):
    """BACKGROUND_EXECUTION='celery' without celery installed raises at validate time."""

    def test_validate_on_ready_raises(self):
        from unittest.mock import patch
        from django_logic.background.settings import validate_on_ready

        celery_cfg = dict(_SYNC_SETTINGS, BACKGROUND_EXECUTION='celery')
        with override_settings(DJANGO_LOGIC=celery_cfg):
            with patch(
                'django_logic.background.settings._celery_available',
                return_value=False,
            ):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    validate_on_ready()
                self.assertIn("'celery' package is not installed", str(ctx.exception))

    def test_validate_on_ready_requires_starter_queue(self):
        bad_cfg = {k: v for k, v in _SYNC_SETTINGS.items() if k != 'STARTER_QUEUE'}
        bad_cfg['BACKGROUND_EXECUTION'] = 'celery'
        with override_settings(DJANGO_LOGIC=bad_cfg):
            # Simulate "celery installed" so the other branch is taken.
            from unittest.mock import patch
            from django_logic.background.settings import validate_on_ready
            with patch(
                'django_logic.background.settings._celery_available',
                return_value=True,
            ):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    validate_on_ready()
                self.assertIn('STARTER_QUEUE', str(ctx.exception))
