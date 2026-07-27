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


    def test_redactor_receives_a_copy_not_the_original(self):
        # Mutating the redactor's argument must not corrupt the live kwargs
        # the transition is still using.
        original = {'amount': 100, 'x': 1}
        with override_settings(DJANGO_LOGIC={
            'LOG_KWARGS_REDACTOR': 'tests.test_log_redaction.drop_amount'
        }):
            redact_log_kwargs(original)
        self.assertEqual(original, {'amount': 100, 'x': 1})

