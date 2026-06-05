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


def _conf() -> dict:
    return getattr(settings, 'DJANGO_LOGIC', {}) or {}


def lock_timeout() -> int:
    return int(_conf().get('LOCK_TIMEOUT', 7200))


def background_execution() -> str:
    """Return the configured execution mode.

    Defaults to ``'celery'`` when Celery is importable, else ``'sync'``.
    Explicit settings override the default.
    """
    configured = _conf().get('BACKGROUND_EXECUTION')
    if configured is None:
        return EXECUTION_CELERY if _celery_available() else EXECUTION_SYNC
    if configured not in _VALID_EXECUTION_MODES:
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['BACKGROUND_EXECUTION'] must be one of "
            f"{sorted(_VALID_EXECUTION_MODES)}; got {configured!r}."
        )
    return configured


def starter_queue() -> str:
    queue = _conf().get('STARTER_QUEUE')
    if not queue:
        raise ImproperlyConfigured(
            "DJANGO_LOGIC['STARTER_QUEUE'] is required. Set it to the "
            "Celery queue where the periodic retry/cleanup tasks should run "
            "(e.g. 'django_logic.starter')."
        )
    return queue


def max_errors() -> int:
    return int(_conf().get('TRANSITION_MESSAGE_MAX_ERRORS', 5))


def retry_minutes() -> int:
    return int(_conf().get('TRANSITION_MESSAGE_RETRY_MINUTES', 2))


def cleanup_days() -> int:
    return int(_conf().get('TRANSITION_MESSAGE_CLEANUP_DAYS', 7))


def sentry_transaction_naming() -> bool:
    """Whether the background runner names/tags the Sentry transaction per
    transition (so each transition is its own issue). Default on; no-op when
    sentry-sdk isn't installed. Set ``DJANGO_LOGIC['SENTRY_TRANSACTION_NAMING']
    = False`` to leave Sentry's own (task-name-based) naming in place."""
    return bool(_conf().get('SENTRY_TRANSACTION_NAMING', True))


def _celery_available() -> bool:
    try:
        import celery  # noqa: F401
    except ImportError:
        return False
    return True


def validate_on_ready() -> None:
    """Called from ``apps.BackgroundConfig.ready`` — fail fast on misconfig."""
    mode = background_execution()
    if mode == EXECUTION_CELERY and not _celery_available():
        raise ImproperlyConfigured(
            "DJANGO_LOGIC['BACKGROUND_EXECUTION']='celery' but the "
            "'celery' package is not installed. Install it "
            "(pip install django-logic[celery]) or set "
            "BACKGROUND_EXECUTION='sync'."
        )
    if mode == EXECUTION_CELERY:
        # Surface STARTER_QUEUE misconfig now rather than on first retry.
        starter_queue()
        _reject_sqlite_in_celery_mode()
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


