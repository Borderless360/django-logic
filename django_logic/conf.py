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

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

LOCK_TIMEOUT_DEFAULT = 7200


def _conf() -> dict:
    return getattr(settings, 'DJANGO_LOGIC', {}) or {}


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
    value = _conf().get('DEFER_UNLOCK_UNTIL_COMMIT', False)
    if not isinstance(value, bool):
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['DEFER_UNLOCK_UNTIL_COMMIT'] must be a bool, "
            f"got {value!r}."
        )
