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
    # Core knobs (LOCK_TIMEOUT, STRICT_HOOK_SIGNATURES) — shared with
    # DjangoLogicConfig.ready so sync-only installs validate them too.
    conf.validate_core_settings()
    if mode == conf.EXECUTION_SYNC:
        _reject_sync_without_opt_in()
    if mode == conf.EXECUTION_PULL:
        # The claim needs real row locks (SKIP LOCKED), and the state lock
        # must span the web process and the worker processes.
        _reject_sqlite_in_pull_mode()
        _check_lock_cache_in_pull_mode()


def _reject_sync_without_opt_in() -> None:
    """Sync runs the worker path inline, in the caller's own thread.

    In a web process that means every side-effect runs inside the
    request, and nothing retries what fails. It is a test runtime, so a
    deployment must not be able to choose it from a settings value or an
    environment variable. A test settings module opts in.
    """
    from django_logic import conf

    if conf.sync_enabled():
        return
    raise ImproperlyConfigured(
        "DJANGO_LOGIC['BACKGROUND_EXECUTION']='sync' runs background "
        "side-effects inline, in the caller's own thread, and nothing "
        "retries what fails. It is a test runtime. Production is always "
        "'pull'. If this is a test settings module, call "
        "django_logic.conf.enable_sync() in it before Django boots. To run "
        "one block inline elsewhere, use django_logic.conf.sync_execution()."
    )


def _reject_sqlite_in_pull_mode() -> None:
    """Pull mode claims rows with SELECT FOR UPDATE SKIP LOCKED, so refuse
    SQLite on the alias that stores ``TransitionMessage``."""
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
            f"alias '{alias}', which uses {engine!r} (SQLite). Point that "
            f"alias at PostgreSQL."
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
