"""Django system checks for django-logic.

Hook-signature validation raises at bind time since 1.0.0, so the old
re-report check is retired: a machine with a bad hook never binds.
"""
from django.core import checks

from django_logic.process import ProcessManager


def _process_tree_has_background_transition(process_class) -> bool:
    """Does ``process_class`` (or any process nested under it) declare a
    background transition? Duck-typed via ``is_background`` so the check
    never imports the background package for the walk itself."""
    from django_logic.process import _iter_process_tree

    return any(
        getattr(transition, 'is_background', False)
        for process_cls in _iter_process_tree(process_class)
        for transition in process_cls.transitions or []
    )


def _models_bound_to_background_transitions() -> list:
    """Sorted labels of the models bound to a process whose tree declares a
    background transition."""
    return sorted({
        binding.model._meta.label for binding in ProcessManager.bindings
        if _process_tree_has_background_transition(binding.process_class)
    })


_LOCAL_CACHE_BACKENDS = (
    'django.core.cache.backends.locmem',
    'django.core.cache.backends.dummy',
)


def _pull_mode_with_background_bindings():
    """The condition the two pull-mode infrastructure checks share.

    Bindings happen in consumer apps' ``ready()`` hooks, so this can only
    be asked after the registry is ready — which is why these rules are
    system checks and not part of the app's own ``ready()``.
    """
    from django_logic import conf

    if conf.background_execution() != conf.EXECUTION_PULL:
        return False
    return bool(_models_bound_to_background_transitions())


@checks.register('django_logic')
def check_pull_mode_database(app_configs, **kwargs):
    """Pull mode claims rows with SELECT FOR UPDATE SKIP LOCKED, so the
    alias that stores ``TransitionMessage`` must not be SQLite
    (``django_logic.E004``). An install with no background transition
    bound never stores a row, so the rule does not apply to it."""
    from django.conf import settings
    from django.db import router

    from django_logic.background.models import TransitionMessage

    if not _pull_mode_with_background_bindings():
        return []
    databases = getattr(settings, 'DATABASES', {}) or {}
    alias = router.db_for_write(TransitionMessage) or 'default'
    engine = (databases.get(alias) or {}).get('ENGINE', '')
    if 'sqlite' not in engine.lower():
        return []
    return [checks.Error(
        f"DJANGO_LOGIC['BACKGROUND_EXECUTION']='pull' requires a database "
        f"that supports SELECT FOR UPDATE with SKIP LOCKED and partial "
        f"unique indexes. TransitionMessage is routed to alias '{alias}', "
        f"which uses {engine!r} (SQLite).",
        hint="Point that alias at PostgreSQL.",
        id='django_logic.E004',
    )]


@checks.register('django_logic')
def check_pull_mode_lock_cache(app_configs, **kwargs):
    """The state lock lives in the ``default`` cache. In pull mode the web
    processes and the worker processes are different OS processes, so a
    per-process cache means the lock silently does not lock anything
    across them (``django_logic.E005``; a warning under ``DEBUG=True`` so
    local pull-mode experiments stay possible)."""
    from django.conf import settings

    if not _pull_mode_with_background_bindings():
        return []
    caches = getattr(settings, 'CACHES', {}) or {}
    backend = (caches.get('default') or {}).get('BACKEND', '')
    if not backend.startswith(_LOCAL_CACHE_BACKENDS):
        return []
    message = (
        f"DJANGO_LOGIC['BACKGROUND_EXECUTION']='pull' but the 'default' "
        f"cache backend is {backend!r}, which is per-process. The state "
        f"lock will not be shared between the web processes and the "
        f"worker processes."
    )
    hint = ("Use a cross-process cache for 'default' — e.g. Django's own "
            "'django.core.cache.backends.redis.RedisCache', or any shared "
            "backend (memcached, django-redis).")
    finding = checks.Warning if getattr(settings, 'DEBUG', False) else checks.Error
    return [finding(message, hint=hint, id='django_logic.E005')]


@checks.register('django_logic')
def check_background_database_routing(app_configs, **kwargs):
    """Database routers must not split the background engine across
    databases (``django_logic.E002``).

    Enqueue writes the instance's ``in_progress_state`` and the
    ``TransitionMessage`` row in ONE transaction. The engine uses plain managers
    and bare ``transaction.atomic()`` throughout, and both resolve to the
    ``default`` alias. A router that sends ``TransitionMessage``, or a model
    bound to a background transition, somewhere else breaks that rule. The state
    write and the row then commit or roll back on their own, so a crash strands
    instances with no row to recover them. Keep ``TransitionMessage`` and every
    background-bound model on the shared ``default`` alias. This check refuses
    anything else.

    It reads static routing only. A router that decides from ``hints``, such as
    an ``instance`` or a ``model_name``, can answer ``default`` to the
    model-class questions asked here and still send a real write elsewhere. So
    passing this check does not prove the rule holds at runtime.
    """
    from django.apps import apps

    # Nothing to route without the app — and nothing here registers
    # without it either. bind_model_process refuses a background binding
    # when the app is missing, so that gap reports itself at bind time.
    if not apps.is_installed('django_logic'):
        return []

    from django.db import DEFAULT_DB_ALIAS, router

    from django_logic.background.models import TransitionMessage

    findings = []
    tm_write = router.db_for_write(TransitionMessage) or DEFAULT_DB_ALIAS
    tm_read = router.db_for_read(TransitionMessage) or DEFAULT_DB_ALIAS
    if tm_write != DEFAULT_DB_ALIAS or tm_read != tm_write:
        findings.append(checks.Error(
            f"A database router routes TransitionMessage to "
            f"write={tm_write!r} / read={tm_read!r}, but the background "
            f"engine's plain managers and bare transaction.atomic() blocks "
            f"resolve to {DEFAULT_DB_ALIAS!r}. The atomic outbox rule — the "
            f"state write and the TransitionMessage row commit in one "
            f"transaction — cannot hold across two databases.",
            hint="Route TransitionMessage (app_label "
                 "'django_logic_background') to the 'default' alias. The "
                 "supported topology is TransitionMessage and every "
                 "background-bound model on the shared 'default' alias.",
            obj='django_logic.background.models.TransitionMessage',
            id='django_logic.E002',
        ))

    seen_models = set()
    for binding in ProcessManager.bindings:
        if binding.model in seen_models:
            continue
        if not _process_tree_has_background_transition(binding.process_class):
            continue
        seen_models.add(binding.model)
        model_write = router.db_for_write(binding.model) or DEFAULT_DB_ALIAS
        if model_write != tm_write:
            findings.append(checks.Error(
                f"{binding.model._meta.label} is bound to a process with "
                f"background transitions but a database router sends its "
                f"writes to {model_write!r} while TransitionMessage writes "
                f"go to {tm_write!r}. The atomic outbox rule — the state "
                f"write and the TransitionMessage row commit in one "
                f"transaction — cannot hold across two databases.",
                hint="Keep every background-bound model on the same "
                     "'default' alias as TransitionMessage. Split databases "
                     "are not supported.",
                obj=binding.model._meta.label,
                id='django_logic.E002',
            ))
    return findings


#: Every key the engine reads. The set is closed, so anything outside it is
#: unread — a typo, or a key a past release removed (the changelog carries
#: the upgrade advice for those).
_KNOWN_SETTINGS = frozenset({
    'BACKGROUND_EXECUTION',
    'DEFAULT_QUEUE',
    'LOCK_TIMEOUT',
    'TRANSITION_MESSAGE_MAX_ERRORS',
    'TRANSITION_MESSAGE_RETRY_MINUTES',
    'TRANSITION_MESSAGE_CLEANUP_DAYS',
})


@checks.register('django_logic')
def check_no_unknown_settings(app_configs, **kwargs):
    """Report ``DJANGO_LOGIC`` keys the engine never reads
    (``django_logic.W004``).

    ``DJANGO_LOGIC`` is a plain dict with no schema. A typo such as
    ``TRANSITION_MESSAGE_MAX_ERROR`` or ``LOCK_TIMOUT`` is ignored and the
    default applies, which is the cause behind every "I set the retry limit and
    it did nothing" report. The known set is closed and small, so listing
    everything outside it is cheap and precise. A key a past release removed
    reports the same way — the value has no effect — and the changelog carries
    that release's upgrade advice.
    """
    from django.conf import settings

    conf = getattr(settings, 'DJANGO_LOGIC', None) or {}
    if not isinstance(conf, dict):
        return []
    unknown = sorted(set(conf) - _KNOWN_SETTINGS)
    if not unknown:
        return []
    return [checks.Warning(
        f"DJANGO_LOGIC contains {'a key' if len(unknown) == 1 else 'keys'} "
        f"django-logic does not read: {', '.join(repr(k) for k in unknown)}. "
        f"The value has no effect and the documented default applies.",
        hint=f"Check for a typo against the documented settings: "
             f"{', '.join(sorted(_KNOWN_SETTINGS))}. A key a past release "
             f"removed should be deleted; the changelog says what replaced "
             f"it. If you keep unrelated keys in DJANGO_LOGIC on purpose, "
             f"silence this with SILENCED_SYSTEM_CHECKS = ['django_logic.W004'].",
        id='django_logic.W004',
    )]
