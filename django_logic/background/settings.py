"""Typed accessors for the ``DJANGO_LOGIC`` settings block.

All reads go through this module so that validation errors surface at
one place and default values are documented once.
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


EXECUTION_CELERY = 'celery'
EXECUTION_SYNC = 'sync'
_VALID_EXECUTION_MODES = frozenset({EXECUTION_CELERY, EXECUTION_SYNC})

STATE_GUARD_ENFORCE = 'enforce'
STATE_GUARD_WARN = 'warn'
_VALID_STATE_GUARD_MODES = frozenset({STATE_GUARD_ENFORCE, STATE_GUARD_WARN})


def _conf() -> dict:
    return getattr(settings, 'DJANGO_LOGIC', {}) or {}


def lock_timeout() -> int:
    return int(_conf().get('LOCK_TIMEOUT', 7200))


def background_execution() -> str:
    """Return the configured execution mode.

    Defaults to ``'celery'`` — background transitions are Celery tasks.
    ``'sync'`` runs phase 2 inline in the same process and exists for
    tests, CI, management commands, and the shell.
    """
    configured = _conf().get('BACKGROUND_EXECUTION')
    if configured is None:
        return EXECUTION_CELERY
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


def starter_queue() -> str:
    """Celery queue for the periodic retry/cleanup safety-net tasks."""
    queue = _conf().get('STARTER_QUEUE', 'django_logic.starter')
    if not queue or not isinstance(queue, str):
        raise ImproperlyConfigured(
            "DJANGO_LOGIC['STARTER_QUEUE'] must be a non-empty string "
            "(the Celery queue where the periodic retry/cleanup tasks run)."
        )
    return queue


def max_errors() -> int:
    return int(_conf().get('TRANSITION_MESSAGE_MAX_ERRORS', 5))


def retry_minutes() -> int:
    return int(_conf().get('TRANSITION_MESSAGE_RETRY_MINUTES', 2))


def cleanup_days() -> int:
    return int(_conf().get('TRANSITION_MESSAGE_CLEANUP_DAYS', 7))


def phase2_state_guard() -> str:
    """How phase 2 reacts when the instance's state no longer matches what
    phase 1 left behind (``in_progress_state``, or a declared source when
    no ``in_progress_state`` exists) — e.g. after a manual ops fix.

    * ``'enforce'`` (default) — mark the row completed as *superseded*,
      skip side-effects, log loudly. The external state change wins.
    * ``'warn'`` — log a warning and run the transition anyway
      (pre-0.4 behaviour).
    """
    mode = _conf().get('PHASE2_STATE_GUARD', STATE_GUARD_ENFORCE)
    if mode not in _VALID_STATE_GUARD_MODES:
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['PHASE2_STATE_GUARD'] must be one of "
            f"{sorted(_VALID_STATE_GUARD_MODES)}; got {mode!r}."
        )
    return mode


def sentry_transaction_naming() -> bool:
    """Whether the background runner names/tags the Sentry transaction per
    transition (so each transition is its own issue). Default on; no-op when
    sentry-sdk isn't installed. Set ``DJANGO_LOGIC['SENTRY_TRANSACTION_NAMING']
    = False`` to leave Sentry's own (task-name-based) naming in place."""
    return bool(_conf().get('SENTRY_TRANSACTION_NAMING', True))


def validate_on_ready() -> None:
    """Called from ``apps.BackgroundConfig.ready`` — fail fast on misconfig."""
    mode = background_execution()
    # Surface value errors now rather than on first use.
    default_queue()
    starter_queue()
    phase2_state_guard()
    if mode == EXECUTION_CELERY:
        _reject_sqlite_in_celery_mode()
        _check_lock_cache_in_celery_mode()
        # NB: broker liveness is NOT checked here — validate_on_ready runs
        # at Django app-ready, which in the standard celery.py pattern is
        # *before* the project's Celery app sets broker_url, so it would
        # false-warn on every boot. The check lives in dispatch (where the
        # app is configured); see dispatch._warn_once_if_no_broker.


def _reject_sqlite_in_celery_mode() -> None:
    """SQLite doesn't support ``select_for_update(nowait=True)`` nor
    partial unique indexes, so the phase-2 concurrency guard silently
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
            f"DJANGO_LOGIC['BACKGROUND_EXECUTION']='celery' requires "
            f"a database that supports select_for_update(nowait=True) "
            f"and partial unique indexes. TransitionMessage is routed to "
            f"alias '{alias}', which uses {engine!r} (SQLite). Switch that "
            f"alias to PostgreSQL or set BACKGROUND_EXECUTION='sync'."
        )


_LOCAL_CACHE_BACKENDS = (
    'django.core.cache.backends.locmem',
    'django.core.cache.backends.dummy',
)


def _check_lock_cache_in_celery_mode() -> None:
    """The state lock lives in the ``default`` cache. In Celery mode the
    web process and the workers are different OS processes (usually
    different hosts), so a local-memory or dummy cache means the lock
    silently does not lock anything across them.

    Production (``DEBUG=False``) fails fast; with ``DEBUG=True`` we only
    warn so local celery-mode experiments stay possible.
    """
    caches = getattr(settings, 'CACHES', {}) or {}
    backend = (caches.get('default') or {}).get('BACKEND', '')
    if not backend.startswith(_LOCAL_CACHE_BACKENDS):
        return
    message = (
        f"DJANGO_LOGIC['BACKGROUND_EXECUTION']='celery' but the 'default' "
        f"cache backend is {backend!r}, which is per-process. The state "
        f"lock will not be shared between web processes and Celery "
        f"workers. Use a cross-process cache (django-redis is installed "
        f"as a django-logic dependency) for the 'default' cache."
    )
    if getattr(settings, 'DEBUG', False):
        from django_logic.logger import logger
        logger.warning(message)
    else:
        raise ImproperlyConfigured(message)
