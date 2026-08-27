"""Every DJANGO_LOGIC key the engine never reads is reported as unread.

DJANGO_LOGIC has no unknown-key rejection, so a typo and a key a past
release removed both fail *open* and silently — the value has no effect
and the default applies. One warning covers both; the changelog carries
each removal's upgrade advice.
"""
from django.test import SimpleTestCase, override_settings

from django_logic.checks import check_no_unknown_settings
from tests import dl_settings


class UnreadSettingsCheckTests(SimpleTestCase):
    def _run(self):
        return check_no_unknown_settings(app_configs=None)

    def test_clean_config_is_silent(self):
        self.assertEqual(self._run(), [])

    @override_settings(DJANGO_LOGIC=dl_settings(
        LOG_KWARGS_REDACTOR='myapp.redact', MY_OWN_KEY=1))
    def test_every_unread_key_is_reported_in_one_warning(self):
        findings = self._run()

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, 'django_logic.unread_setting')
        self.assertIn('LOG_KWARGS_REDACTOR', findings[0].msg)
        self.assertIn('MY_OWN_KEY', findings[0].msg)
        self.assertIn('no effect', findings[0].msg)
        self.assertIn('changelog', findings[0].hint)

    @override_settings(DJANGO_LOGIC=None)
    def test_a_missing_or_odd_setting_does_not_crash_the_check_run(self):
        self.assertEqual(self._run(), [])
