"""Core ``DJANGO_LOGIC`` settings — readers and boot-time validation.

These knobs are consumed by the core engine (state locks, transition
unlock semantics) independently of the optional
``django_logic.background`` app, so their validation cannot live only in
the background app's ready hook: a sync-only install that registers just
``django_logic`` must fail fast on misconfiguration too.
``DjangoLogicConfig.ready`` calls :func:`validate_core_settings`;
``django_logic.background.settings.validate_on_ready`` calls the same
function as part of its full safety gate (both paths are idempotent —
pure reads, no state).
"""
import math
import os

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

LOCK_TIMEOUT_DEFAULT = 7200


def _conf() -> dict:
    conf = getattr(settings, 'DJANGO_LOGIC', None)
    if conf is None:
        # Unset — every reader falls back to its documented default.
        return {}
    if not isinstance(conf, dict):
        # Every reader does conf.get(...), so a string or a list of keys
        # used to surface as a bare AttributeError from whichever reader
        # ran first — during AppConfig.ready(), with no mention of the
        # setting that was wrong.
        raise ImproperlyConfigured(
            f'DJANGO_LOGIC must be a dict, got '
            f'{type(conf).__name__} ({conf!r}).'
        )
    return conf


def lock_timeout():
    """Effective global ``LOCK_TIMEOUT`` in seconds — read on every call
    (not cached at import time)."""
    return _conf().get('LOCK_TIMEOUT', LOCK_TIMEOUT_DEFAULT)


def defer_unlock_until_commit() -> bool:
    """Strict runtime reader for ``DEFER_UNLOCK_UNTIL_COMMIT``: only a
    literal ``True`` enables deferral. The setting gates lock-release
    semantics, so truthy garbage (``'false'``, ``1``) must not flip it —
    boot validation rejects non-bools, and this reader stays safe even
    where that validation has not run."""
    return _conf().get('DEFER_UNLOCK_UNTIL_COMMIT', False) is True


def strict_hook_signatures() -> bool:
    """Strict reader for ``STRICT_HOOK_SIGNATURES`` — literal ``True`` only,
    same reasoning as :func:`defer_unlock_until_commit`. Read at bind time
    by ``process._validate_hook_signatures``."""
    return _conf().get('STRICT_HOOK_SIGNATURES', False) is True


def transition_coverage_log():
    """Path the transition-coverage recorder appends to, or ``None`` when
    recording is off. Validated by :func:`validate_core_settings`."""
    return _conf().get('TRANSITION_COVERAGE_LOG') or None


def validate_core_settings() -> None:
    """Fail fast on misconfigured core knobs (``ImproperlyConfigured``
    naming the setting), from every install shape."""
    value = _conf().get('LOCK_TIMEOUT', LOCK_TIMEOUT_DEFAULT)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['LOCK_TIMEOUT'] must be a positive finite number "
            f"of seconds (it is the lock TTL and the liveness signal for "
            f"stranded-state recovery), got {value!r}."
        )
    # Both strict flags are read with `is True`, so truthy garbage disables
    # them rather than enabling — silent in the UNSAFE direction. Validated
    # here (not in background/settings) because STRICT_HOOK_SIGNATURES is a
    # core setting read from process.py at bind time, with no background app
    # involved, so a sync-only install must be covered too.
    for key in ('DEFER_UNLOCK_UNTIL_COMMIT', 'STRICT_HOOK_SIGNATURES'):
        value = _conf().get(key, False)
        if not isinstance(value, bool):
            raise ImproperlyConfigured(
                f"DJANGO_LOGIC[{key!r}] must be a bool (True or False), got "
                f"{value!r}. Strings are not accepted — 'false' would "
                f"otherwise read as truthy."
            )
    # The coverage log goes straight to open(path, 'a'), where a bool is not
    # a type error: open(True) writes to file descriptor 1, so
    # TRANSITION_COVERAGE_LOG = True silently appends coverage lines to
    # stdout. Anything that is not a path is refused at boot, where it is
    # attributable, rather than per transition inside a swallowed observer
    # exception.
    value = _conf().get('TRANSITION_COVERAGE_LOG')
    if value is not None and not isinstance(value, (str, os.PathLike)):
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['TRANSITION_COVERAGE_LOG'] must be a filesystem "
            f"path (str or os.PathLike), or None to disable recording, got "
            f"{value!r}."
        )
