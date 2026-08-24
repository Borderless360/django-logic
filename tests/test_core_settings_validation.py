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
