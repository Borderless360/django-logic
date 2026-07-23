"""Typed accessors for the ``DJANGO_LOGIC`` settings block.

All reads go through this module so that validation errors surface at
one place and default values are documented once.
"""
from __future__ import annotations

import math

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
    """Celery queue for the periodic retry/cleanup safety-net tasks.

    Consumed by :func:`beat_schedule`, which routes the four periodic
    tasks here. A hand-written ``CELERY_BEAT_SCHEDULE`` must set
    ``options={'queue': ...}`` itself — the framework does not intercept
    task routing.
    """
    queue = _conf().get('STARTER_QUEUE', 'django_logic.starter')
    if not queue or not isinstance(queue, str):
        raise ImproperlyConfigured(
            "DJANGO_LOGIC['STARTER_QUEUE'] must be a non-empty string "
            "(the Celery queue where the periodic retry/cleanup tasks run)."
        )
    return queue


def beat_schedule(
    *,
    retry_seconds: float = 60.0,
    detect_stuck_seconds: float = 300.0,
    watchdog_seconds: float = 120.0,
    cleanup_seconds: float = 86_400.0,
    stranded_seconds: float = 300.0,
) -> dict:
    """Ready-made Celery beat entries for the five safety-net tasks,
    routed to ``DJANGO_LOGIC['STARTER_QUEUE']``.

    Use it from your project's ``celery.py`` (after the app is configured)
    so the safety net cannot be forgotten or routed to the wrong queue::

        from django_logic.background import beat_schedule
        app.conf.beat_schedule = {**app.conf.beat_schedule, **beat_schedule()}

    The intervals are overridable per task; the defaults match the
    README's recommended schedule.
    """
    queue = starter_queue()

    def entry(task: str, seconds: float) -> dict:
        return {'task': task, 'schedule': seconds, 'options': {'queue': queue}}

    return {
        'django-logic-retry-stale': entry(
            'django_logic.retry_stale_transitions', retry_seconds),
        'django-logic-detect-stuck': entry(
            'django_logic.detect_stuck_transitions', detect_stuck_seconds),
        'django-logic-watchdog': entry(
            'django_logic.watchdog_stale_attempts', watchdog_seconds),
        'django-logic-recover-stranded': entry(
            'django_logic.recover_stranded_states', stranded_seconds),
        'django-logic-cleanup': entry(
            'django_logic.cleanup_completed_transitions', cleanup_seconds),
    }


def _validated_number(
    key: str,
    default,
    *,
    minimum,
    allow_zero: bool = True,
    integral: bool = False,
):
    """Read ``DJANGO_LOGIC[key]`` and validate it is a sane number.

    Raises ``ImproperlyConfigured`` naming the setting and the offending
    value. ``bool`` is rejected explicitly (it subclasses ``int``, so
    ``True`` would otherwise pass as ``1``); non-finite floats (``nan``,
    ``inf``) are rejected; ``integral=True`` additionally rejects
    non-integral floats and returns an ``int``.

    ``allow_zero=False`` makes the bound strict: the value must be
    ``> minimum`` rather than ``>= minimum``.
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
    if allow_zero:
        if value < minimum:
            raise ImproperlyConfigured(
                f"DJANGO_LOGIC[{key!r}] must be >= {minimum}, got {value!r}."
            )
    elif value <= minimum:
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC[{key!r}] must be > {minimum}, got {value!r}."
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
    """Age (minutes) before the periodic starter re-dispatches an
    uncompleted row. Must be >= 0; zero means "retry immediately" and is
    used by tests to drive the starter without back-dating rows."""
    return _validated_number(
        'TRANSITION_MESSAGE_RETRY_MINUTES', 2, minimum=0)


def cleanup_days():
    """Age (days) before completed rows are deleted by the periodic
    cleanup. Must be >= 0. Zero deletes every completed row on the next
    cleanup tick — that erases the audit trail, so it is test-only."""
    return _validated_number(
        'TRANSITION_MESSAGE_CLEANUP_DAYS', 7, minimum=0)


def lock_timeout():
    """Boot-time validation gate for ``DJANGO_LOGIC['LOCK_TIMEOUT']`` —
    the state-lock TTL in seconds. Must be a finite number > 0 (a zero
    or negative TTL means the lock never holds and every transition's
    mutual exclusion silently disappears).

    ``django_logic.state`` keeps its own private per-call reader; this
    accessor exists so :func:`validate_on_ready` rejects a bad value at
    boot rather than on the first lock attempt.
    """
    return _validated_number('LOCK_TIMEOUT', 7200, minimum=0, allow_zero=False)


def process_class_aliases() -> dict:
    """``DJANGO_LOGIC['PROCESS_CLASS_ALIASES']`` — escape hatch for
    renaming/moving a Process class while rows recorded under its old
    dotted path are still in flight (#140).

    A dict mapping old dotted path -> new dotted path, applied by the
    phase-2 restore before importing a recorded ``process_class``.
    Default ``{}``.
    """
    aliases = _conf().get('PROCESS_CLASS_ALIASES', {})
    if aliases is None:
        return {}
    if not isinstance(aliases, dict) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in aliases.items()
    ):
        raise ImproperlyConfigured(
            "DJANGO_LOGIC['PROCESS_CLASS_ALIASES'] must be a dict mapping "
            "old dotted process-class paths (str) to new dotted paths (str)."
        )
    return aliases


def _validate_defer_unlock_until_commit() -> None:
    """``DEFER_UNLOCK_UNTIL_COMMIT`` gates lock-release semantics; truthy
    garbage (``'false'``, ``1``) reads as enabled/disabled by accident,
    so require a real bool. The runtime reader lives in
    ``django_logic.transition``; this is the boot-time gate."""
    value = _conf().get('DEFER_UNLOCK_UNTIL_COMMIT', False)
    if not isinstance(value, bool):
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['DEFER_UNLOCK_UNTIL_COMMIT'] must be a bool, "
            f"got {value!r}."
        )


def _validate_log_kwargs_redactor() -> None:
    """``LOG_KWARGS_REDACTOR`` (if set) must be a callable or an
    importable dotted path. ``redact_log_kwargs`` degrades a broken
    redactor to a ``__redaction_error__`` marker at runtime (it must
    never break a transition), which means a typo'd dotted path silently
    ruins every log line — so import it once here and fail at boot."""
    redactor = _conf().get('LOG_KWARGS_REDACTOR')
    if redactor is None:
        return
    if isinstance(redactor, str):
        from django.utils.module_loading import import_string
        try:
            import_string(redactor)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"DJANGO_LOGIC['LOG_KWARGS_REDACTOR'] ({redactor!r}) is not "
                f"an importable dotted path: {exc}. A broken redactor "
                f"degrades every transition log line to a redaction marker."
            ) from exc
        return
    if not callable(redactor):
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['LOG_KWARGS_REDACTOR'] must be a callable or a "
            f"dotted path to one, got {redactor!r}."
        )


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
    # Safety settings (#149): every numeric knob the retry/cleanup/lock
    # machinery depends on is validated at boot in EVERY mode — a bad
    # value must not wait for its first use (which may be a 3am retry
    # tick) to explode, or worse, silently misbehave.
    max_errors()
    retry_minutes()
    cleanup_days()
    lock_timeout()
    process_class_aliases()
    _validate_defer_unlock_until_commit()
    _validate_log_kwargs_redactor()
    if mode == EXECUTION_CELERY:
        _reject_sqlite_in_celery_mode()
        _check_lock_cache_in_celery_mode()
        # NB: broker liveness is NOT checked here — validate_on_ready runs
        # at Django app-ready, which in the standard celery.py pattern is
        # *before* the project's Celery app sets broker_url, so it would
        # false-warn on every boot. The check lives in dispatch (where the
        # app is configured); see dispatch._warn_once_about_celery_config.


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


def strict_kwargs_serialization() -> bool:
    """When True, phase-1 kwargs serialization raises on silently-droppable
    caller kwargs (``request``) instead of logging a warning.

    Default False: generic API layers commonly pass ``request`` to every
    transition uniformly, so raising by default would break them. Enable
    once call sites are clean to turn the drop into a hard contract.
    """
    return bool(_conf().get('STRICT_KWARGS_SERIALIZATION', False))
