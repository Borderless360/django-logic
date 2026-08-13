"""Removed DJANGO_LOGIC keys are reported by the unknown-key check (W004).

DJANGO_LOGIC has no unknown-key rejection, so every removal fails *open* and
silently. The case that motivated the report: a deployment that set
LOG_KWARGS_REDACTOR for PII compliance upgrades and starts writing raw kwargs
to its logs, with nothing anywhere saying so.
"""
from django.test import SimpleTestCase, override_settings

from django_logic.checks import _REMOVED_SETTINGS, check_no_unknown_settings
from tests import dl_settings


class RemovedSettingsCheckTests(SimpleTestCase):
    def _run(self):
        return check_no_unknown_settings(app_configs=None)

    def test_clean_config_is_silent(self):
        self.assertEqual(self._run(), [])

    def test_every_removed_key_is_reported(self):
        for key in _REMOVED_SETTINGS:
            with self.subTest(key=key):
                with override_settings(DJANGO_LOGIC=dl_settings(**{key: 'x'})):
                    findings = self._run()
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].id, 'django_logic.W004')
                self.assertIn(key, findings[0].msg)
                self.assertIn('removed', findings[0].msg)

    @override_settings(DJANGO_LOGIC=dl_settings(
        LOG_KWARGS_REDACTOR='myapp.redact', PHASE2_STATE_GUARD='warn'))
    def test_several_keys_are_reported_separately(self):
        findings = self._run()

        self.assertEqual(len(findings), 2)
        self.assertEqual({f.id for f in findings}, {'django_logic.W004'})

    @override_settings(DJANGO_LOGIC=dl_settings(LOG_KWARGS=False))
    def test_the_redaction_case_names_the_replacement(self):
        # The whole point: say where scrubbing moved to, not just "removed".
        self.assertIn('logging.Filter', self._run()[0].msg)

    @override_settings(DJANGO_LOGIC=None)
    def test_a_missing_or_odd_setting_does_not_crash_the_check_run(self):
        self.assertEqual(self._run(), [])
