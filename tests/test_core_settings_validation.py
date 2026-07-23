"""Core-knob validation must not depend on the background app.

``LOCK_TIMEOUT`` and ``DEFER_UNLOCK_UNTIL_COMMIT`` are consumed by the
core engine (state locks, unlock semantics) whether or not
``django_logic.background`` is installed. ``DjangoLogicConfig.ready``
calls ``django_logic.conf.validate_core_settings()`` so a sync-only
install fails fast too; and the runtime reader for
``DEFER_UNLOCK_UNTIL_COMMIT`` is strict — only a literal ``True``
changes lock-release semantics, so truthy garbage that slipped past a
never-run boot gate still cannot flip it.
"""
import math

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from django_logic.conf import (
    defer_unlock_until_commit,
    lock_timeout,
    validate_core_settings,
)
from django_logic.process import Process
from django_logic.state import State
from django_logic.transition import Transition
from tests.models import Invoice

_BASE = {'BACKGROUND_EXECUTION': 'sync'}


def _conf(**overrides):
    return dict(_BASE, **overrides)


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

    def test_defer_unlock_rejections(self):
        for bad in ('false', 'true', 1, 0, None):
            with self.subTest(value=bad):
                self.assert_rejected(
                    _conf(DEFER_UNLOCK_UNTIL_COMMIT=bad),
                    'DEFER_UNLOCK_UNTIL_COMMIT')

    def test_valid_values_accepted(self):
        with override_settings(DJANGO_LOGIC=_conf(
            LOCK_TIMEOUT=0.5, DEFER_UNLOCK_UNTIL_COMMIT=True,
        )):
            validate_core_settings()
            self.assertEqual(lock_timeout(), 0.5)
            self.assertIs(defer_unlock_until_commit(), True)

    def test_defaults_accepted_with_empty_conf(self):
        with override_settings(DJANGO_LOGIC={}):
            validate_core_settings()
            self.assertEqual(lock_timeout(), 7200)
            self.assertIs(defer_unlock_until_commit(), False)

    def test_core_app_ready_runs_the_gate(self):
        """The gate fires from the CORE AppConfig — a sync-only install
        (no django_logic.background) fails fast at boot too."""
        config = apps.get_app_config('django_logic')
        with override_settings(DJANGO_LOGIC=_conf(LOCK_TIMEOUT='bad')):
            with self.assertRaises(ImproperlyConfigured):
                config.ready()
        # And a healthy configuration keeps ready() a no-op re-run.
        config.ready()


class StrictDeferReaderTests(TestCase):
    """Runtime behavior with garbage that bypassed boot validation: only
    a literal True defers — 'false' must NOT silently enable deferral."""

    def test_truthy_string_does_not_enable_deferral(self):
        with override_settings(DJANGO_LOGIC=_conf(
            DEFER_UNLOCK_UNTIL_COMMIT='false',
        )):
            self.assertIs(defer_unlock_until_commit(), False)

            class _P(Process):
                process_name = 'strict_defer_process'
                transitions = [
                    Transition('approve', sources=['draft'], target='approved'),
                ]

            invoice = Invoice.objects.create(status='draft')
            _P(field_name='status', instance=invoice).approve()
            # Deferral did NOT engage: unlocked immediately even inside
            # the test's atomic block.
            self.assertFalse(State(invoice, 'status').is_locked())
            self.assertEqual(invoice.status, 'approved')
