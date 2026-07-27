"""``redact_log_kwargs`` — what gets attached to a transition log record.

0.10.0 removed the ``LOG_KWARGS`` / ``LOG_KWARGS_REDACTOR`` opt-ins (no
consumer ever set them; scrubbing belongs in a ``logging.Filter``). The shallow
copy is the part that stays, and it is a real fix rather than a knob: records
format lazily while the caller keeps mutating kwargs.
"""
from django.test import SimpleTestCase

from django_logic.logger import redact_log_kwargs


class RedactLogKwargsTests(SimpleTestCase):
    def test_kwargs_are_logged_as_is(self):
        kw = {'user': 'u', 'amount': 100}
        self.assertEqual(redact_log_kwargs(kw), kw)

    def test_returns_a_copy_so_later_mutation_cannot_leak_in(self):
        # Log records are formatted lazily and the caller keeps mutating
        # kwargs after the log call (restore_user pops user_id, nested
        # transitions rewrite tr_id/parent_id). Sharing the reference would
        # let those later mutations appear in an already-emitted record.
        original = {'amount': 100, 'x': 1}

        attached = redact_log_kwargs(original)
        original['amount'] = 999
        original['added_later'] = True

        self.assertIsNot(attached, original)
        self.assertEqual(attached, {'amount': 100, 'x': 1})
