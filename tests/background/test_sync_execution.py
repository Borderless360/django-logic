"""End to end: BackgroundTransition and BackgroundAction in sync mode.

The test suite runs in sync mode by default (see tests/settings.py), so
``instance.process.fulfil()`` enqueues and executes inline. The tests can then
assert on the final state directly.
"""
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from django_logic.background import sync_execution
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from tests.background.models import Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)


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
        transition_message = TransitionMessage.objects.get(
            app_label='bg_tests',
            model_name='widget',
            instance_id=self.widget.pk,
        )
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 0)
        self.assertEqual(transition_message.queue_name,
                         'django_logic.critical')

    def test_queue_name_persisted(self):
        self.widget.status = 'fulfilled'
        self.widget.save()
        self.widget.process.generate_export()
        transition_message = TransitionMessage.objects.get(
            transition_name='generate_export')
        self.assertEqual(transition_message.queue_name, 'django_logic.slow')

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
        # Success callbacks run for a BackgroundAction too. The only
        # difference from a BackgroundTransition is the skipped state write.
        self.assertIn('cb,', self.widget.cb_log)

    def test_action_records_transition_message(self):
        self.widget.process.sync_inventory()
        transition_message = TransitionMessage.objects.get(
            transition_name='sync_inventory')
        self.assertTrue(transition_message.is_completed)


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
        transition_message = TransitionMessage.objects.get(
            transition_name='crash')
        self.assertFalse(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 1)
        self.assertEqual(transition_message.last_error_message, 'boom')
        # The state stays in in_progress_state because a retry is still due.
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'crashing')

    def test_reaches_max_errors_and_writes_failed_state(self):
        # Allow one error only, so the first failure is terminal.
        with override_settings(
            DJANGO_LOGIC=dict(_SYNC_SETTINGS, TRANSITION_MESSAGE_MAX_ERRORS=1)
        ):
            with self.assertRaises(ValueError):
                self.widget.process.crash()
        transition_message = TransitionMessage.objects.get(
            transition_name='crash')
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 1)
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
        # The first enqueue committed — the row exists and the state is
        # 'fulfilling' — but the worker has not completed it.
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
            # Put the instance back on a declared source so the source gate
            # passes and the row is what rejects the second call.
            fresh.status = 'draft'
            fresh.save()
            fresh.process.fulfil()

    def test_non_guard_integrity_error_surfaces_raw(self):
        # An IntegrityError from the user's own model write — here the
        # in_progress_state write — must not be relabelled as
        # AlreadyInProgress. Only the partial unique constraint on
        # TransitionMessage means "already in progress", and the row is
        # inserted first so that constraint is the only one that can fire.
        from unittest.mock import patch
        from django.db import IntegrityError

        widget = Widget.objects.create()
        with patch(
            'django_logic.state.State.set_state',
            side_effect=IntegrityError('CHECK constraint failed: status'),
        ):
            with self.assertRaises(IntegrityError):
                widget.process.fulfil()
        # And the rolled-back atomic left no orphan TransitionMessage.
        self.assertFalse(
            TransitionMessage.objects.filter(
                instance_id=str(widget.pk), is_completed=False
            ).exists()
        )


class SyncExecutionContextManagerTests(TestCase):
    """sync_execution() forces sync mode even when the global setting is
    'pull'."""

    def test_context_manager_overrides_setting(self):
        pull_cfg = dict(_SYNC_SETTINGS, BACKGROUND_EXECUTION='pull')
        with override_settings(DJANGO_LOGIC=pull_cfg):
            widget = Widget.objects.create()
            with sync_execution():
                widget.process.fulfil()
            widget.refresh_from_db()
            self.assertEqual(widget.status, 'fulfilled')


class ValidateOnReadyTests(TestCase):
    """Boot-time validation of the pull-mode deployment contract."""

    def test_execution_mode_defaults_to_pull(self):
        # Workers claim committed rows from the database unless the project
        # opts into sync mode for tests or CI.
        from django_logic import conf as bg_settings

        cfg = {k: v for k, v in _SYNC_SETTINGS.items()
               if k != 'BACKGROUND_EXECUTION'}
        with override_settings(DJANGO_LOGIC=cfg):
            self.assertEqual(
                bg_settings.background_execution(),
                bg_settings.EXECUTION_PULL,
            )

    def test_an_unknown_mode_is_refused_naming_the_valid_ones(self):
        from django_logic import conf as bg_settings

        cfg = dict(_SYNC_SETTINGS, BACKGROUND_EXECUTION='celery')
        with override_settings(DJANGO_LOGIC=cfg):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                bg_settings.background_execution()
            self.assertIn('pull', str(ctx.exception))
            self.assertIn('sync', str(ctx.exception))

    def test_validate_on_ready_rejects_sqlite_in_pull_mode(self):
        from django_logic.background.apps import validate_on_ready

        pull_cfg = dict(_SYNC_SETTINGS, BACKGROUND_EXECUTION='pull')
        sqlite_db = {
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': ':memory:',
            }
        }
        with override_settings(DJANGO_LOGIC=pull_cfg, DATABASES=sqlite_db):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                validate_on_ready()
            self.assertIn('SQLite', str(ctx.exception))
            self.assertIn('PostgreSQL', str(ctx.exception))

    def test_validate_on_ready_rejects_locmem_cache_in_pull_mode(self):
        # A per-process cache locks nothing across web processes and workers,
        # so boot must fail instead of running unprotected in production.
        from django_logic.background.apps import validate_on_ready

        pull_cfg = dict(_SYNC_SETTINGS, BACKGROUND_EXECUTION='pull')
        pg_db = {
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': 'x',
            }
        }
        locmem = {
            'default': {
                'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            }
        }
        with override_settings(
            DJANGO_LOGIC=pull_cfg, DATABASES=pg_db, CACHES=locmem, DEBUG=False
        ):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                validate_on_ready()
            self.assertIn('per-process', str(ctx.exception))

    def test_locmem_cache_in_pull_mode_only_warns_with_debug(self):
        from django_logic.background.apps import validate_on_ready

        pull_cfg = dict(_SYNC_SETTINGS, BACKGROUND_EXECUTION='pull')
        pg_db = {
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': 'x',
            }
        }
        locmem = {
            'default': {
                'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            }
        }
        with override_settings(
            DJANGO_LOGIC=pull_cfg, DATABASES=pg_db, CACHES=locmem, DEBUG=True
        ):
            with self.assertLogs('django-logic', level='WARNING') as logs:
                validate_on_ready()  # must not raise
            self.assertTrue(
                any('per-process' in line for line in logs.output)
            )
