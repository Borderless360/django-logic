"""Removed DJANGO_LOGIC keys are reported as W003, typos as W004.

DJANGO_LOGIC has no unknown-key rejection, so every removal fails *open* and
silently. The case that motivated the report: a deployment that set
LOG_KWARGS_REDACTOR for PII compliance upgrades and starts writing raw kwargs
to its logs, with nothing anywhere saying so.

One function makes both reports, but the ids stay separate. The typo hint
tells you to silence W004 when you keep extra keys on purpose; if the
migration advice shared that id, following the hint would hide it.
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
                self.assertEqual(findings[0].id, 'django_logic.W003')
                self.assertIn(key, findings[0].msg)
                self.assertIn('removed', findings[0].msg)

    @override_settings(DJANGO_LOGIC=dl_settings(
        LOG_KWARGS_REDACTOR='myapp.redact', PHASE2_STATE_GUARD='warn'))
    def test_several_keys_are_reported_separately(self):
        findings = self._run()

        self.assertEqual(len(findings), 2)
        self.assertEqual({f.id for f in findings}, {'django_logic.W003'})

    @override_settings(DJANGO_LOGIC=dl_settings(
        LOG_KWARGS_REDACTOR='myapp.redact', MY_OWN_KEY=1))
    def test_a_removed_key_and_a_typo_keep_separate_ids(self):
        # Silencing one must never silence the other. The typo hint tells the
        # reader to silence W004 for keys they keep on purpose; a shared id
        # would hide the migration advice from everyone who did that.
        findings = self._run()

        self.assertEqual(
            {f.id for f in findings},
            {'django_logic.W003', 'django_logic.W004'},
        )
        removed = next(f for f in findings if f.id == 'django_logic.W003')
        typo = next(f for f in findings if f.id == 'django_logic.W004')
        self.assertIn('LOG_KWARGS_REDACTOR', removed.msg)
        self.assertIn('MY_OWN_KEY', typo.msg)
        self.assertNotIn('SILENCED_SYSTEM_CHECKS', removed.hint)

    @override_settings(DJANGO_LOGIC=dl_settings(LOG_KWARGS=False))
    def test_the_redaction_case_names_the_replacement(self):
        # The whole point: say where scrubbing moved to, not just "removed".
        self.assertIn('logging.Filter', self._run()[0].msg)

    @override_settings(DJANGO_LOGIC=None)
    def test_a_missing_or_odd_setting_does_not_crash_the_check_run(self):
        self.assertEqual(self._run(), [])
