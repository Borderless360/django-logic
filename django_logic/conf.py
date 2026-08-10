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


def legacy_exception_base():
    """Dotted path of an extra base class to mix into
    ``TransitionNotAllowed`` (coexistence with a differently-named fork
    during a migration, #190), or ``None``."""
    return _conf().get('LEGACY_EXCEPTION_BASE') or None


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
    """Mix the settings-declared legacy base into ``TransitionNotAllowed``
    (#190). Idempotent — called from both apps' ``ready()``.

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
        # BaseException, not Exception (#196): a fork __init__ that raises
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
        # serialize exception info (#196). `in`, not equality, for str(): a
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
