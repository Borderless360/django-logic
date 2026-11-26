from django.core.exceptions import ImproperlyConfigured
from django.test import (
    SimpleTestCase,
    override_settings,
)

from tests import dl_settings

def _conf(**overrides):
    return dl_settings(**overrides)


class SyncIsATestRuntimeTests(SimpleTestCase):
    """Sync runs the worker path inline, in the caller's own thread. A
    deployment must not be able to choose it from a settings value or an
    environment variable, so boot refuses it unless a test settings
    module opted in."""

    def test_boot_refuses_sync_without_the_opt_in(self):
        from unittest.mock import patch

        from django_logic.conf import validate_on_ready

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
        from django_logic.conf import validate_on_ready
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
