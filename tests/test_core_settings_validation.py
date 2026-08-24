"""Core-knob validation must not depend on the background app.

``LOCK_TIMEOUT`` is consumed by the core engine (state locks) whether or
not ``django_logic.background`` is installed. ``DjangoLogicConfig.ready``
calls ``django_logic.conf.validate_core_settings()`` so a sync-only
install fails fast too.
"""
import math

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from django_logic.conf import (
    lock_timeout,
    validate_core_settings,
)
from tests import dl_settings

def _conf(**overrides):
    return dl_settings(**overrides)


class CoreSettingsValidationTests(SimpleTestCase):
    def assert_rejected(self, conf, setting_name):
        with override_settings(DJANGO_LOGIC=conf):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                validate_core_settings()
        self.assertIn(setting_name, str(ctx.exception))

    def test_lock_timeout_rejections(self):
        for bad in ('7200', True, math.nan, math.inf, 0, -5):
            with self.subTest(value=bad):
                self.assert_rejected(_conf(LOCK_TIMEOUT=bad), 'LOCK_TIMEOUT')

    def test_valid_values_accepted(self):
        with override_settings(DJANGO_LOGIC=_conf(LOCK_TIMEOUT=0.5)):
            validate_core_settings()
            self.assertEqual(lock_timeout(), 0.5)

    def test_defaults_accepted_with_empty_conf(self):
        with override_settings(DJANGO_LOGIC={}):
            validate_core_settings()
            self.assertEqual(lock_timeout(), 7200)

    def test_core_app_ready_runs_the_gate(self):
        """The gate fires from the CORE AppConfig — a sync-only install
        (no django_logic.background) fails fast at boot too."""
        config = apps.get_app_config('django_logic')
        with override_settings(DJANGO_LOGIC=_conf(LOCK_TIMEOUT='bad')):
            with self.assertRaises(ImproperlyConfigured):
                config.ready()
        # And a healthy configuration keeps ready() a no-op re-run.
        config.ready()


class SyncIsATestRuntimeTests(SimpleTestCase):
    """Sync runs the worker path inline, in the caller's own thread. A
    deployment must not be able to choose it from a settings value or an
    environment variable, so boot refuses it unless a test settings
    module opted in."""

    def test_boot_refuses_sync_without_the_opt_in(self):
        from unittest.mock import patch

        from django_logic.background.apps import validate_on_ready

        with patch('django_logic.conf._sync_enabled', False), \
                override_settings(DJANGO_LOGIC=_conf(
                    BACKGROUND_EXECUTION='sync')):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                validate_on_ready()
        message = str(ctx.exception)
        self.assertIn('test runtime', message)
        self.assertIn('enable_sync', message)

    def test_boot_accepts_sync_after_the_opt_in(self):
        """tests/settings.py calls enable_sync(), which is how the whole
        suite runs inline."""
        from django_logic.background.apps import validate_on_ready
        from django_logic.conf import sync_enabled

        self.assertTrue(sync_enabled())
        with override_settings(DJANGO_LOGIC=_conf(
                BACKGROUND_EXECUTION='sync')):
            validate_on_ready()

    def test_one_block_runs_inline_without_the_opt_in(self):
        from unittest.mock import patch

        from django_logic.conf import sync_execution, sync_mode

        with patch('django_logic.conf._sync_enabled', False), \
                override_settings(DJANGO_LOGIC=_conf(
                    BACKGROUND_EXECUTION='pull')):
            self.assertFalse(sync_mode())
            with sync_execution():
                self.assertTrue(sync_mode())
