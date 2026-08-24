"""Django system checks for django-logic.

Validation at bind time warns through the transition logger during
``AppConfig.ready()``, which runs before test and development logging is
configured, so a consumer can miss the warning. The checks framework runs
after setup, and ``manage.py check``, every test run and deploy checks report
it whatever the logging configuration is.
"""
from django.core import checks

from django_logic.process import (
    ProcessManager,
    collect_hook_signature_offenders,
)


@checks.register('django_logic')
def check_hook_signatures(app_configs, **kwargs):
    """Re-run hook-signature validation over every bound machine
    (``django_logic.W001``)."""
    findings = []
    seen = set()
    for binding in ProcessManager.bindings:
        for offender in collect_hook_signature_offenders(binding.process_class):
            key = (binding.model, binding.process_class, offender)
            if key in seen:
                continue
            seen.add(key)
            findings.append(checks.Warning(
                f'FSM hook without a named instance-first parameter: {offender}',
                hint='The engine calls hooks as fn(instance, **kwargs) '
                     '(permissions as fn(instance, user, **kwargs)); give the '
                     'hook a named first parameter. Decorated hooks need '
                     'functools.wraps to expose the real signature.',
                obj=f'{binding.model._meta.label} ({binding.process_class.__name__})',
                id='django_logic.W001',
            ))
    return findings


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


@checks.register('django_logic')
def check_background_app_is_installed(app_configs, **kwargs):
    """A bound ``BackgroundTransition`` needs ``django_logic.background`` in
    ``INSTALLED_APPS`` (``django_logic.E003``).

    The app owns the ``TransitionMessage`` table and its migrations. Without it,
    enqueue has nowhere to write the row, so the first background transition
    fails with ``OperationalError: no such table`` from deep inside the engine.
    The other background checks also skip an install without the app, so
    ``manage.py check`` used to report nothing at all.
    """
    from django.apps import apps

    if apps.is_installed('django_logic.background'):
        return []
    offenders = _models_bound_to_background_transitions()
    if not offenders:
        return []
    return [checks.Error(
        "Background transitions are bound on %s, but "
        "'django_logic.background' is not in INSTALLED_APPS. The app owns "
        "the TransitionMessage table those transitions write in enqueue, so "
        "the first background transition fails with a missing-table "
        "database error." % ', '.join(offenders),
        hint="Add 'django_logic.background' to INSTALLED_APPS and run "
             "manage.py migrate. There is no table-less mode: use plain "
             "synchronous Transitions where the durable outbox is not "
             "wanted.",
        id='django_logic.E003',
    )]


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

    # Nothing to route without the app. A bound background transition with the
    # app missing is its own error, reported by the check above.
    if not apps.is_installed('django_logic.background'):
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


_REMOVED_SETTINGS = {
    'LOG_KWARGS':
        'kwargs are always attached to log records now; scrub them with a '
        'logging.Filter on the "django-logic.transition" logger',
    'LOG_KWARGS_REDACTOR':
        'kwargs are always attached to log records now; scrub them with a '
        'logging.Filter on the "django-logic.transition" logger',
    'PHASE2_STATE_GUARD':
        'the worker state guard always enforces; there are no modes',
    'SENTRY_TRANSACTION_NAMING':
        'Sentry transactions are always named per transition',
    'PROCESS_CLASS_ALIASES':
        'let pending rows complete before renaming a Process class',
    'TRANSITION_COVERAGE_LOG':
        'transition-coverage recording was removed in 0.14.0',
    'STARTER_QUEUE':
        'the safety nets run inside the pull worker loop; nothing is '
        'scheduled on a queue anymore',
}


#: Every key the engine reads. The set is closed, so anything outside it (and
#: outside ``_REMOVED_SETTINGS``) is a typo.
_KNOWN_SETTINGS = frozenset({
    'BACKGROUND_EXECUTION',
    'DEFAULT_QUEUE',
    'LEGACY_EXCEPTION_BASE',
    'LOCK_TIMEOUT',
    'DEFER_UNLOCK_UNTIL_COMMIT',
    'STRICT_HOOK_SIGNATURES',
    'STRICT_KWARGS_SERIALIZATION',
    'TRANSITION_MESSAGE_MAX_ERRORS',
    'TRANSITION_MESSAGE_RETRY_MINUTES',
    'TRANSITION_MESSAGE_CLEANUP_DAYS',
})


@checks.register('django_logic')
def check_no_unknown_settings(app_configs, **kwargs):
    """Report ``DJANGO_LOGIC`` keys the engine never reads — a key a past
    release removed (``django_logic.W003``) or a typo
    (``django_logic.W004``).

    ``DJANGO_LOGIC`` is a plain dict with no schema. A typo such as
    ``TRANSITION_MESSAGE_MAX_ERROR`` or ``LOCK_TIMOUT`` is ignored and the
    default applies, which is the cause behind every "I set the retry limit and
    it did nothing" report. The known set is closed and small, so listing
    everything outside it is cheap and precise.

    A key a past release removed gets its own warning, carrying the upgrade
    advice from ``_REMOVED_SETTINGS`` instead of the typo hint. One function
    reports both, and the two ids stay separate on purpose. The typo hint tells
    you how to silence it when you keep extra keys deliberately, and that must
    not silence the upgrade advice as well.
    """
    from django.conf import settings

    conf = getattr(settings, 'DJANGO_LOGIC', None) or {}
    if not isinstance(conf, dict):
        return []
    messages = [
        checks.Warning(
            f"DJANGO_LOGIC['{key}'] was removed and is now ignored: "
            f"{advice}.",
            hint='Delete the key from DJANGO_LOGIC.',
            id='django_logic.W003',
        )
        for key, advice in _REMOVED_SETTINGS.items() if key in conf
    ]
    unknown = sorted(
        set(conf) - _KNOWN_SETTINGS - set(_REMOVED_SETTINGS)
    )
    if unknown:
        messages.append(checks.Warning(
            f"DJANGO_LOGIC contains {'a key' if len(unknown) == 1 else 'keys'} "
            f"django-logic does not read: {', '.join(repr(k) for k in unknown)}. "
            f"The value has no effect and the documented default applies.",
            hint=f"Check for a typo against the documented settings: "
                 f"{', '.join(sorted(_KNOWN_SETTINGS))}. If you keep unrelated "
                 f"keys in DJANGO_LOGIC on purpose, silence this with "
                 f"SILENCED_SYSTEM_CHECKS = ['django_logic.W004'].",
            id='django_logic.W004',
        ))
    return messages
