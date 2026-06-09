"""DJANGO_LOGIC logging-privacy controls for transition kwargs."""
from django.test import SimpleTestCase, override_settings

from django_logic.logger import redact_log_kwargs


def drop_amount(kwargs):
    """Module-level redactor, referenced by dotted path in a test."""
    kwargs.pop('amount', None)
    return kwargs


def _boom(kwargs):
    raise ValueError('redactor blew up')


class RedactLogKwargsTests(SimpleTestCase):
    def test_default_logs_kwargs_as_is(self):
        kw = {'user': 'u', 'amount': 100}
        self.assertEqual(redact_log_kwargs(kw), kw)

    @override_settings(DJANGO_LOGIC={'LOG_KWARGS': False})
    def test_disabled_returns_empty(self):
        self.assertEqual(redact_log_kwargs({'amount': 100}), {})

    @override_settings(DJANGO_LOGIC={
        'LOG_KWARGS_REDACTOR': 'tests.test_log_redaction.drop_amount'
    })
    def test_redactor_by_dotted_path(self):
        out = redact_log_kwargs({'amount': 100, 'x': 1})
        self.assertNotIn('amount', out)
        self.assertEqual(out['x'], 1)

    def test_redactor_as_callable(self):
        with override_settings(DJANGO_LOGIC={
            'LOG_KWARGS_REDACTOR': lambda kw: {'redacted': True}
        }):
            self.assertEqual(redact_log_kwargs({'a': 1}), {'redacted': True})

    def test_redactor_receives_a_copy_not_the_original(self):
        # Mutating the redactor's argument must not corrupt the live kwargs
        # the transition is still using.
        original = {'amount': 100, 'x': 1}
        with override_settings(DJANGO_LOGIC={
            'LOG_KWARGS_REDACTOR': 'tests.test_log_redaction.drop_amount'
        }):
            redact_log_kwargs(original)
        self.assertEqual(original, {'amount': 100, 'x': 1})

    def test_broken_redactor_degrades_safely(self):
        with override_settings(DJANGO_LOGIC={
            'LOG_KWARGS_REDACTOR': 'tests.test_log_redaction._boom'
        }):
            self.assertEqual(
                redact_log_kwargs({'a': 1}), {'__redaction_error__': True}
            )
