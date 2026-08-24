from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def validate_on_ready() -> None:
    """The background app's boot gate — fail fast on misconfig."""
    from django_logic import conf

    mode = conf.background_execution()
    # Surface value errors now rather than on first use.
    conf.default_queue()
    # Safety settings: every numeric knob the retry/cleanup/lock
    # machinery depends on is validated at boot in EVERY mode — a bad
    # value must not wait for its first use (which may be a 3am retry
    # tick) to explode, or worse, silently misbehave.
    conf.max_errors()
    conf.retry_minutes()
    conf.cleanup_days()
    conf.validate_bool('STRICT_KWARGS_SERIALIZATION')
    # Core knobs (LOCK_TIMEOUT, DEFER_UNLOCK_UNTIL_COMMIT) — shared with
    # DjangoLogicConfig.ready so sync-only installs validate them too.
    conf.validate_core_settings()
    if mode == conf.EXECUTION_PULL:
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


class BackgroundConfig(AppConfig):
    name = 'django_logic.background'
    label = 'django_logic_background'
    verbose_name = 'Django Logic — Background Transitions'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self) -> None:
        validate_on_ready()
        from django_logic import checks  # noqa: F401 — registers system checks
        from django_logic.conf import install_legacy_exception_base

        # Idempotent — covers an install shape where only the background
        # app's ready() runs before denials are raised or caught.
        install_legacy_exception_base()
