"""Boot-time validation of every safety setting (#149).

The retry/cleanup/lock machinery silently misbehaves on garbage values
(``MAX_ERRORS=0`` finalizes before the first attempt, ``True`` reads as
``1``, ``NaN`` poisons every comparison, a negative ``LOCK_TIMEOUT``
means the lock never holds). ``validate_on_ready`` must reject them all
at boot, in every execution mode, with an error naming the setting.
"""
import math

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from django_logic.background.settings import (
    cleanup_days,
    lock_timeout,
    max_errors,
    process_class_aliases,
    retry_minutes,
    validate_on_ready,
)


_BASE = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


def _conf(**overrides):
    return dict(_BASE, **overrides)


class SettingValidationTests(SimpleTestCase):
    """Each knob: invalid type (string, bool, NaN), out-of-range, and the
    allowed zero boundary — all through validate_on_ready()."""

    def assert_rejected(self, conf, setting_name):
        with override_settings(DJANGO_LOGIC=conf):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                validate_on_ready()
        self.assertIn(setting_name, str(ctx.exception))

    def assert_accepted(self, conf):
        with override_settings(DJANGO_LOGIC=conf):
            validate_on_ready()  # must not raise

    # -- TRANSITION_MESSAGE_MAX_ERRORS (whole number >= 1) ------------------

    def test_max_errors_rejects_string(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_MAX_ERRORS='5'),
            'TRANSITION_MESSAGE_MAX_ERRORS')

    def test_max_errors_rejects_bool(self):
        # bool subclasses int; True must NOT pass as 1.
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_MAX_ERRORS=True),
            'TRANSITION_MESSAGE_MAX_ERRORS')

    def test_max_errors_rejects_nan(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_MAX_ERRORS=math.nan),
            'TRANSITION_MESSAGE_MAX_ERRORS')

    def test_max_errors_rejects_non_integral_float(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_MAX_ERRORS=2.5),
            'TRANSITION_MESSAGE_MAX_ERRORS')

    def test_max_errors_rejects_zero(self):
        # Zero would finalize a row before its first attempt ever ran.
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_MAX_ERRORS=0),
            'TRANSITION_MESSAGE_MAX_ERRORS')

    def test_max_errors_rejects_negative(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_MAX_ERRORS=-1),
            'TRANSITION_MESSAGE_MAX_ERRORS')

    def test_max_errors_accepts_minimum_one_and_integral_float(self):
        self.assert_accepted(_conf(TRANSITION_MESSAGE_MAX_ERRORS=1))
        with override_settings(
            DJANGO_LOGIC=_conf(TRANSITION_MESSAGE_MAX_ERRORS=3.0)
        ):
            self.assertEqual(max_errors(), 3)
            self.assertIsInstance(max_errors(), int)

    # -- TRANSITION_MESSAGE_RETRY_MINUTES (number >= 0) ----------------------

    def test_retry_minutes_rejects_string(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_RETRY_MINUTES='2'),
            'TRANSITION_MESSAGE_RETRY_MINUTES')

    def test_retry_minutes_rejects_bool(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_RETRY_MINUTES=False),
            'TRANSITION_MESSAGE_RETRY_MINUTES')

    def test_retry_minutes_rejects_nan_and_infinity(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_RETRY_MINUTES=math.nan),
            'TRANSITION_MESSAGE_RETRY_MINUTES')
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_RETRY_MINUTES=math.inf),
            'TRANSITION_MESSAGE_RETRY_MINUTES')

    def test_retry_minutes_rejects_negative(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_RETRY_MINUTES=-1),
            'TRANSITION_MESSAGE_RETRY_MINUTES')

    def test_retry_minutes_accepts_zero(self):
        # Zero = immediate retry; the test suites rely on it.
        self.assert_accepted(_conf(TRANSITION_MESSAGE_RETRY_MINUTES=0))
        with override_settings(
            DJANGO_LOGIC=_conf(TRANSITION_MESSAGE_RETRY_MINUTES=0)
        ):
            self.assertEqual(retry_minutes(), 0)

    # -- TRANSITION_MESSAGE_CLEANUP_DAYS (number >= 0) -----------------------

    def test_cleanup_days_rejects_string(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_CLEANUP_DAYS='7'),
            'TRANSITION_MESSAGE_CLEANUP_DAYS')

    def test_cleanup_days_rejects_bool(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_CLEANUP_DAYS=True),
            'TRANSITION_MESSAGE_CLEANUP_DAYS')

    def test_cleanup_days_rejects_nan(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_CLEANUP_DAYS=math.nan),
            'TRANSITION_MESSAGE_CLEANUP_DAYS')

    def test_cleanup_days_rejects_negative(self):
        self.assert_rejected(
            _conf(TRANSITION_MESSAGE_CLEANUP_DAYS=-0.5),
            'TRANSITION_MESSAGE_CLEANUP_DAYS')

    def test_cleanup_days_accepts_zero(self):
        # Zero = delete completed rows on the next tick (test-only).
        self.assert_accepted(_conf(TRANSITION_MESSAGE_CLEANUP_DAYS=0))
        with override_settings(
            DJANGO_LOGIC=_conf(TRANSITION_MESSAGE_CLEANUP_DAYS=0)
        ):
            self.assertEqual(cleanup_days(), 0)

    # -- LOCK_TIMEOUT (number > 0) -------------------------------------------

    def test_lock_timeout_rejects_string(self):
        self.assert_rejected(_conf(LOCK_TIMEOUT='7200'), 'LOCK_TIMEOUT')

    def test_lock_timeout_rejects_bool(self):
        self.assert_rejected(_conf(LOCK_TIMEOUT=True), 'LOCK_TIMEOUT')

    def test_lock_timeout_rejects_nan(self):
        self.assert_rejected(_conf(LOCK_TIMEOUT=math.nan), 'LOCK_TIMEOUT')

    def test_lock_timeout_rejects_zero(self):
        # A zero TTL means the lock never holds — mutual exclusion gone.
        self.assert_rejected(_conf(LOCK_TIMEOUT=0), 'LOCK_TIMEOUT')

    def test_lock_timeout_rejects_negative(self):
        self.assert_rejected(_conf(LOCK_TIMEOUT=-5), 'LOCK_TIMEOUT')

    def test_lock_timeout_accepts_positive_float(self):
        self.assert_accepted(_conf(LOCK_TIMEOUT=0.5))
        with override_settings(DJANGO_LOGIC=_conf(LOCK_TIMEOUT=0.5)):
            self.assertEqual(lock_timeout(), 0.5)

    # -- DEFER_UNLOCK_UNTIL_COMMIT (real bool) --------------------------------

    def test_defer_unlock_rejects_truthy_garbage(self):
        for garbage in ('false', 1, 0, None):
            with self.subTest(value=garbage):
                self.assert_rejected(
                    _conf(DEFER_UNLOCK_UNTIL_COMMIT=garbage),
                    'DEFER_UNLOCK_UNTIL_COMMIT')

    def test_defer_unlock_accepts_real_bools(self):
        self.assert_accepted(_conf(DEFER_UNLOCK_UNTIL_COMMIT=True))
        self.assert_accepted(_conf(DEFER_UNLOCK_UNTIL_COMMIT=False))

    # -- PROCESS_CLASS_ALIASES (dict[str, str]) --------------------------------

    def test_aliases_reject_non_dict(self):
        self.assert_rejected(
            _conf(PROCESS_CLASS_ALIASES=[('old', 'new')]),
            'PROCESS_CLASS_ALIASES')

    def test_aliases_reject_non_string_entries(self):
        self.assert_rejected(
            _conf(PROCESS_CLASS_ALIASES={'old.path.Cls': 42}),
            'PROCESS_CLASS_ALIASES')
        self.assert_rejected(
            _conf(PROCESS_CLASS_ALIASES={42: 'new.path.Cls'}),
            'PROCESS_CLASS_ALIASES')

    def test_aliases_accept_str_str_dict_and_default_empty(self):
        self.assert_accepted(
            _conf(PROCESS_CLASS_ALIASES={'old.path.Cls': 'new.path.Cls'}))
        with override_settings(DJANGO_LOGIC=_conf()):
            self.assertEqual(process_class_aliases(), {})

    # -- LOG_KWARGS_REDACTOR (importable dotted path / callable) --------------

    def test_redactor_rejects_unimportable_dotted_path(self):
        self.assert_rejected(
            _conf(LOG_KWARGS_REDACTOR='tests.no.such.module.redact'),
            'LOG_KWARGS_REDACTOR')

    def test_redactor_rejects_non_callable(self):
        self.assert_rejected(
            _conf(LOG_KWARGS_REDACTOR=42), 'LOG_KWARGS_REDACTOR')

    def test_redactor_accepts_importable_path_and_callable(self):
        self.assert_accepted(
            _conf(LOG_KWARGS_REDACTOR='tests.test_log_redaction.drop_amount'))
        self.assert_accepted(
            _conf(LOG_KWARGS_REDACTOR=lambda kw: {}))

    def test_unset_optional_settings_validate_clean(self):
        # The documented defaults ({} aliases, False defer, no redactor)
        # must pass without the keys present at all.
        self.assert_accepted(_conf())
