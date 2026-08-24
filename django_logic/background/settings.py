"""Typed accessors for the ``DJANGO_LOGIC`` settings block.

All reads go through this module so that validation errors surface at
one place and default values are documented once.
"""
from __future__ import annotations

import math

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

# The core reader, shared rather than re-implemented: it validates that
# DJANGO_LOGIC is a dict, which every ``.get()`` below assumes — a string or
# a list used to surface as a bare AttributeError raised from whichever
# ready() hook read a setting first, naming nothing.
from django_logic.conf import _conf

EXECUTION_SYNC = 'sync'
EXECUTION_PULL = 'pull'
_VALID_EXECUTION_MODES = frozenset({EXECUTION_SYNC, EXECUTION_PULL})


def background_execution() -> str:
    """Return the configured execution mode.

    Defaults to ``'pull'`` — workers claim committed rows from the
    database (run them with ``manage.py dl_worker``). ``'sync'`` runs the
    worker inline in the same process and exists for tests, CI,
    management commands, and the shell.
    """
    configured = _conf().get('BACKGROUND_EXECUTION')
    if configured is None:
        return EXECUTION_PULL
    if configured == 'celery':
        raise ImproperlyConfigured(
            "DJANGO_LOGIC['BACKGROUND_EXECUTION']='celery' was removed: "
            "workers now claim committed rows from the database, so no "
            "broker carries them. Set 'pull' and run one "
            "`manage.py dl_worker --queues <names>` process per queue "
            "group (the worker loop also runs the safety nets, so no "
            "beat schedule is needed). Drain the old broker queues once "
            "before switching. See the README's background section."
        )
    if configured not in _VALID_EXECUTION_MODES:
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['BACKGROUND_EXECUTION'] must be one of "
            f"{sorted(_VALID_EXECUTION_MODES)}; got {configured!r}."
        )
    return configured


def default_queue() -> str:
    """Queue used by background transitions that don't declare ``queue=``.

    Per-transition ``queue=`` overrides this; use it to route work to
    dedicated workers (e.g. ``critical`` / ``slow``) and manage
    performance per queue.
    """
    queue = _conf().get('DEFAULT_QUEUE', 'django_logic')
    if not queue or not isinstance(queue, str):
        raise ImproperlyConfigured(
            "DJANGO_LOGIC['DEFAULT_QUEUE'] must be a non-empty string."
        )
    return queue


def _validated_number(
    key: str,
    default,
    *,
    minimum,
    integral: bool = False,
):
    """Read ``DJANGO_LOGIC[key]`` and validate it is a sane number.

    Raises ``ImproperlyConfigured`` naming the setting and the offending
    value. ``bool`` is rejected explicitly (it subclasses ``int``, so
    ``True`` would otherwise pass as ``1``); non-finite floats (``nan``,
    ``inf``) are rejected; ``integral=True`` additionally rejects
    non-integral floats and returns an ``int``. ``minimum`` is inclusive.
    """
    value = _conf().get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC[{key!r}] must be a number, got {value!r}."
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC[{key!r}] must be a finite number, got {value!r}."
        )
    if integral and isinstance(value, float) and not value.is_integer():
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC[{key!r}] must be a whole number, got {value!r}."
        )
    if value < minimum:
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC[{key!r}] must be >= {minimum}, got {value!r}."
        )
    if integral:
        return int(value)
    return value


def max_errors() -> int:
    """Attempts before a background transition is finalized as failed.
    Must be a whole number >= 1 (0 would finalize before the first
    attempt ever ran)."""
    return _validated_number(
        'TRANSITION_MESSAGE_MAX_ERRORS', 5, minimum=1, integral=True)


def retry_minutes():
    """Minutes a failed row stays unclaimable after ``last_error_dt``.
    The pull claim's ``WHERE`` clause is the retry rule: nothing
    re-dispatches a row; it simply becomes claimable again after this
    wait. Must be >= 0; zero means "claimable immediately" and is used
    by tests to retry without back-dating rows."""
    return _validated_number(
        'TRANSITION_MESSAGE_RETRY_MINUTES', 2, minimum=0)


def cleanup_days():
    """Age (days) before completed rows are deleted by the periodic
    cleanup. Must be >= 0. Zero deletes every completed row on the next
    cleanup tick — that erases the audit trail, so it is test-only."""
    return _validated_number(
        'TRANSITION_MESSAGE_CLEANUP_DAYS', 7, minimum=0)


def validate_on_ready() -> None:
    """Called from ``apps.BackgroundConfig.ready`` — fail fast on misconfig."""
    mode = background_execution()
    # Surface value errors now rather than on first use.
    default_queue()
    # Safety settings: every numeric knob the retry/cleanup/lock
    # machinery depends on is validated at boot in EVERY mode — a bad
    # value must not wait for its first use (which may be a 3am retry
    # tick) to explode, or worse, silently misbehave.
    max_errors()
    retry_minutes()
    cleanup_days()
    _validate_bool('STRICT_KWARGS_SERIALIZATION')
    # Core knobs (LOCK_TIMEOUT, DEFER_UNLOCK_UNTIL_COMMIT) — shared with
    # DjangoLogicConfig.ready so sync-only installs validate them too.
    from django_logic.conf import validate_core_settings
    validate_core_settings()
    if mode == EXECUTION_PULL:
        # The claim needs real row locks (SKIP LOCKED), and the state lock
        # must span the web process and the worker processes.
        _reject_sqlite_in_pull_mode()
        _check_lock_cache_in_pull_mode()


def _reject_sqlite_in_pull_mode() -> None:
    """SQLite doesn't support ``select_for_update(nowait=True)`` nor
    partial unique indexes, so the worker concurrency guard silently
    degrades to "serialize everything" — which masks real bugs in dev
    and fails in prod.

    Only the alias that actually stores ``TransitionMessage`` is checked:
    a Postgres-default deployment with an unrelated secondary SQLite alias
    (a legacy read-only DB, a fixture/import DB) is fine. Read
    ``settings.DATABASES`` directly (not ``django.db.connections``) so
    tests using ``override_settings(DATABASES=...)`` are reflected.
    """
    from django.db import router

    from django_logic.background.models import TransitionMessage

    databases = getattr(settings, 'DATABASES', {}) or {}
    alias = router.db_for_write(TransitionMessage) or 'default'
    engine = (databases.get(alias) or {}).get('ENGINE', '')
    if 'sqlite' in engine.lower():
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['BACKGROUND_EXECUTION']='pull' requires "
            f"a database that supports SELECT FOR UPDATE with SKIP LOCKED "
            f"and partial unique indexes. TransitionMessage is routed to "
            f"alias '{alias}', which uses {engine!r} (SQLite). Switch that "
            f"alias to PostgreSQL or set BACKGROUND_EXECUTION='sync'."
        )


_LOCAL_CACHE_BACKENDS = (
    'django.core.cache.backends.locmem',
    'django.core.cache.backends.dummy',
)


def _check_lock_cache_in_pull_mode() -> None:
    """The state lock lives in the ``default`` cache. In Pull mode the
    web process and the workers are different OS processes (usually
    different hosts), so a local-memory or dummy cache means the lock
    silently does not lock anything across them.

    Production (``DEBUG=False``) fails fast; with ``DEBUG=True`` we only
    warn so local pull-mode experiments stay possible.
    """
    caches = getattr(settings, 'CACHES', {}) or {}
    backend = (caches.get('default') or {}).get('BACKEND', '')
    if not backend.startswith(_LOCAL_CACHE_BACKENDS):
        return
    message = (
        f"DJANGO_LOGIC['BACKGROUND_EXECUTION']='pull' but the 'default' "
        f"cache backend is {backend!r}, which is per-process. The state "
        f"lock will not be shared between the web processes and the "
        f"worker processes. Use a cross-process cache for 'default' — "
        f"e.g. 'django.core.cache.backends.redis.RedisCache', or "
        f"django-redis via `pip install django-logic[redis]`."
    )
    if getattr(settings, 'DEBUG', False):
        from django_logic.logger import logger
        logger.warning(message)
    else:
        raise ImproperlyConfigured(message)


def _validate_bool(key: str) -> None:
    """Reject a non-bool on a setting that gates behaviour.

    Truthiness coercion is unsafe for these: the strings 'false'/'no'/'0'
    are all truthy, so a value meant to disable a feature enabled it.
    """
    value = _conf().get(key, False)
    if not isinstance(value, bool):
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC[{key!r}] must be a bool (True or False), got "
            f"{value!r}. Strings are not accepted — 'false' would otherwise "
            f"read as truthy and enable the very behaviour it names."
        )


def strict_kwargs_serialization() -> bool:
    """When True, enqueue kwargs serialization raises on silently-droppable
    caller kwargs (``request``) instead of logging a warning.

    Default False: generic API layers commonly pass ``request`` to every
    transition uniformly, so raising by default would break them. Enable
    once call sites are clean to turn the drop into a hard contract.

    Only a literal ``True`` enables it. It used to be
    ``bool(...)``-coerced, so any non-empty string switched strict mode ON —
    reading ``DL_STRICT=false`` from an env var made enqueue start raising.
    Mirrors ``conf.defer_unlock_until_commit``; boot validation rejects
    non-bools and this reader stays safe where that has not run.
    """
    return _conf().get('STRICT_KWARGS_SERIALIZATION', False) is True
