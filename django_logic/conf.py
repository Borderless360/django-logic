"""Every ``DJANGO_LOGIC`` settings reader, and boot-time validation.

One module owns every key: one reader per key, one number validator,
and one place where each default is written down.

Validation runs from one boot gate: ``DjangoLogicConfig.ready`` calls
``validate_on_ready``, which validates every key in every mode. The
pull-mode deployment requirements (database, cache) are the
``pull_mode_needs_postgresql`` and ``pull_mode_needs_a_shared_cache``
system checks instead, because they
depend on bindings that happen after ``ready()`` runs. All of it is
idempotent — pure reads, no state.
"""
import math
from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

LOCK_TIMEOUT_DEFAULT = 7200

EXECUTION_SYNC = 'sync'
EXECUTION_PULL = 'pull'
_VALID_EXECUTION_MODES = frozenset({EXECUTION_SYNC, EXECUTION_PULL})


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


#: Set by :func:`enable_sync`. Boot reads it to decide whether
#: ``BACKGROUND_EXECUTION='sync'`` is allowed in this process.
_sync_enabled = False


def enable_sync() -> None:
    """Allow ``BACKGROUND_EXECUTION='sync'`` in this process.

    Sync runs the worker path inline, in the caller's own thread. That
    is a test runtime, not a deployment: in a web process it runs every
    side-effect inside the request, and nothing retries what fails.
    Production is always pull.

    Call this from a test settings module, before Django boots::

        from django_logic.conf import enable_sync

        enable_sync()
        DJANGO_LOGIC = {'BACKGROUND_EXECUTION': 'sync'}

    It lives here, not in ``django_logic.testing``, because a settings
    module is imported before the app registry is ready and that package
    imports models.

    A single block can run inline without this — use
    :func:`sync_execution`.
    """
    global _sync_enabled
    _sync_enabled = True


def sync_enabled() -> bool:
    """Whether :func:`enable_sync` ran in this process."""
    return _sync_enabled


def background_execution() -> str:
    """Return the configured execution mode.

    Defaults to ``'pull'`` — workers claim committed rows from the
    database (run them with ``manage.py dl_worker``). ``'sync'`` runs the
    worker inline in the same process; it is a test runtime and boot
    refuses it unless :func:`enable_sync` ran.
    """
    configured = _conf().get('BACKGROUND_EXECUTION')
    if configured is None:
        return EXECUTION_PULL
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


def retry_window_minutes() -> int:
    """The whole retry pipeline's span plus slack, floored so a short test
    retry config does not classify a fresh row as stale."""
    return max(retry_minutes() * (max_errors() + 1), 15)


_force_sync: ContextVar[bool] = ContextVar('_dl_force_sync', default=False)


@contextmanager
def sync_execution():
    """Force sync mode for the duration of the ``with`` block.

    Useful inside a test / management command when the global setting
    is ``'pull'`` but you want the worker path to run inline for this
    block.
    """
    token = _force_sync.set(True)
    try:
        yield
    finally:
        _force_sync.reset(token)


def sync_mode() -> bool:
    """Whether the worker path runs inline right now: a
    :func:`sync_execution` block is active, or ``BACKGROUND_EXECUTION``
    is ``'sync'``."""
    return _force_sync.get() or background_execution() == EXECUTION_SYNC


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
            f"of seconds (it is the state lock's TTL, so it bounds how long a "
            f"crashed run can keep an instance locked), got {value!r}."
        )
