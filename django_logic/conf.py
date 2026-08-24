"""Every ``DJANGO_LOGIC`` settings reader, and boot-time validation.

One module owns every key: one reader per key, one number validator,
one bool validator, and one place where each default is written down.

Validation runs from two boot gates. ``DjangoLogicConfig.ready`` calls
:func:`validate_core_settings` — the keys the core engine reads with or
without the background app installed. The background app's ready hook
(``django_logic.background.apps``) validates the background keys and
the pull-mode deployment requirements on top. Both paths are idempotent
— pure reads, no state.
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


def strict_hook_signatures() -> bool:
    """Strict reader for ``STRICT_HOOK_SIGNATURES``: only a literal
    ``True`` enables it. Truthy garbage (``'false'``, ``1``) must not flip
    a behaviour gate — boot validation rejects non-bools, and this reader
    stays safe even where that validation has not run. Read at bind time
    by ``process._validate_hook_signatures``."""
    return _conf().get('STRICT_HOOK_SIGNATURES', False) is True


def legacy_exception_base():
    """Dotted path of an extra base class to mix into
    ``TransitionNotAllowed`` (coexistence with a differently-named fork
    during a migration), or ``None``."""
    return _conf().get('LEGACY_EXCEPTION_BASE') or None


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


def validate_bool(key: str) -> None:
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
    Boot validation rejects non-bools and this reader stays safe where
    that has not run.
    """
    return _conf().get('STRICT_KWARGS_SERIALIZATION', False) is True


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
    # The strict flags are read with `is True`, so truthy garbage disables
    # them rather than enabling — silent in the UNSAFE direction. Validated
    # here because STRICT_HOOK_SIGNATURES is a core setting read from
    # process.py at bind time, with no background app involved, so a
    # sync-only install must be covered too.
    validate_bool('STRICT_HOOK_SIGNATURES')
    # Type check only — resolving the dotted path imports consumer code,
    # which does not belong in a pure-read validator. The import happens in
    # install_legacy_exception_base(), where failures are equally loud and
    # equally attributable to the setting.
    value = _conf().get('LEGACY_EXCEPTION_BASE')
    if value is not None and (not isinstance(value, str) or not value):
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['LEGACY_EXCEPTION_BASE'] must be the dotted path "
            f"of an exception class (str), or None, got {value!r}."
        )


def install_legacy_exception_base() -> None:
    """Mix the settings-declared legacy base into ``TransitionNotAllowed``. Idempotent — called from both apps' ``ready()``.

    A consumer migrating off a differently-named fork must run both engines
    side by side, with shared handlers that catch the *fork's*
    ``TransitionNotAllowed``. Declaring the fork's class here makes this
    engine's denials also instances of it, so those handlers keep answering
    gracefully instead of turning into 500s. Zero cost when unset — which is
    every consumer not mid-migration. Every failure mode raises
    ``ImproperlyConfigured`` at boot: a broken bridge must never be silent,
    that is the whole point of supporting it first-class.
    """
    path = legacy_exception_base()
    if not path:
        return
    from django.utils.module_loading import import_string

    from django_logic.exceptions import TransitionNotAllowed

    try:
        base = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['LEGACY_EXCEPTION_BASE'] could not be imported "
            f"({path!r}): {exc}"
        )
    if not (isinstance(base, type) and issubclass(base, BaseException)):
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['LEGACY_EXCEPTION_BASE'] must name an exception "
            f"class, got {base!r}."
        )
    if base is TransitionNotAllowed or issubclass(TransitionNotAllowed, base):
        # Already bridged (double ready()) or an existing ancestor.
        return
    original_bases = TransitionNotAllowed.__bases__
    try:
        TransitionNotAllowed.__bases__ = original_bases + (base,)
    except TypeError as exc:
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['LEGACY_EXCEPTION_BASE'] {path!r} cannot be mixed "
            f"into TransitionNotAllowed (incompatible bases/MRO): {exc}"
        )
    # Smoke-construct through the new MRO. Neither TransitionNotAllowed nor
    # DjangoLogicException defines __init__, so a legacy base with a
    # non-message signature would otherwise boot green and then replace
    # every denial with a TypeError at its raise site.
    probe_msg = 'legacy-base constructor compatibility probe'
    try:
        probe = TransitionNotAllowed(probe_msg)
    except BaseException as exc:
        # BaseException, not Exception: a fork __init__ that raises
        # SystemExit/KeyboardInterrupt during boot must not leave the class
        # half-mutated. Unwind always; translate only Exception.
        TransitionNotAllowed.__bases__ = original_bases
        if not isinstance(exc, Exception):
            raise
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['LEGACY_EXCEPTION_BASE'] {path!r} breaks "
            f"TransitionNotAllowed('message') construction "
            f"({type(exc).__name__}: {exc}). The legacy base must accept a "
            f"single message argument, like a plain Exception subclass."
        )
    if probe.args != (probe_msg,) or probe_msg not in str(probe):
        # A message-eating base (the `self.message = message;
        # super().__init__()` idiom) blanks str() and args for every denial,
        # and args=() breaks exception (un)pickling wherever celery/tblib
        # serialize exception info. `in`, not equality, for str(): a
        # fork __str__ that FORMATS the preserved message (prefixes, codes)
        # is a working bridge, not a broken one.
        TransitionNotAllowed.__bases__ = original_bases
        raise ImproperlyConfigured(
            f"DJANGO_LOGIC['LEGACY_EXCEPTION_BASE'] {path!r} alters the "
            f"denial message (args={probe.args!r}, str={str(probe)!r}). "
            f"Denial text and exception pickling depend on args, so the "
            f"legacy base must pass the message through to Exception like "
            f"a plain Exception subclass."
        )
