"""Django system checks for django-logic.

Bind-time validation warns through the transition logger, which runs
during ``AppConfig.ready()`` — before test/dev logging is configured, so
warn-mode consumers can miss it entirely. The checks framework runs after
setup and is surfaced by ``manage.py check``, every test run, and deploy
checks, regardless of logging configuration.
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


# django_logic.E001 (shared in_progress_state with divergent recovery) was
# retired in 0.12.0 along with recover_stranded_states. The check existed to
# scope the sweep: a record-less stranded instance carried no provenance, so
# transitions sharing a marker had to agree on how to recover it. With
# in_progress_state now background-only — written atomically with the
# TransitionMessage row — every marked instance has its exact transition on
# the row, recovery is TM-scoped, and sharing a marker is harmless.


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

    The app owns the ``TransitionMessage`` table and its migrations. Without
    it, phase 1 has nowhere to write the outbox row, so the first background
    transition dies with a raw ``OperationalError: no such table`` from deep
    inside the engine — and the other background checks (E002, W002) gate on
    the very same missing app, so ``manage.py check`` said nothing at all.
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
        "the TransitionMessage table those transitions write in phase 1, so "
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
    databases (``django_logic.E002``, #148).

    The durability contract is an *atomic outbox*: phase 1 writes the
    instance's ``in_progress_state`` and the ``TransitionMessage`` row in
    ONE transaction, and the runtime uses unqualified managers and bare
    ``transaction.atomic()`` throughout — both resolve to the ``default``
    alias. A router that sends ``TransitionMessage`` (or a background-bound
    model) elsewhere silently breaks that invariant: the state write and
    the outbox row commit (or roll back) independently, so a crash strands
    instances with no durable record — exactly what the engine exists to
    prevent. The supported topology is ``TransitionMessage`` and every
    background-bound model on the shared ``default`` alias; anything else
    is refused here at check time.

    Static routing only: a router that decides from ``hints`` (an
    ``instance``, a ``model_name``) can answer ``default`` to the
    model-class questions asked here and still send a real write elsewhere,
    so passing this check is not proof the invariant holds at runtime.
    """
    from django.apps import apps

    # Nothing to route without the app; a bound background transition with
    # the app missing is django_logic.E003, not a routing finding.
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
            f"engine's unqualified managers and bare transaction.atomic() "
            f"blocks resolve to {DEFAULT_DB_ALIAS!r}. The atomic outbox "
            f"invariant (state write + TransitionMessage row in one "
            f"transaction) cannot hold across databases.",
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
                f"go to {tm_write!r}. The atomic outbox invariant (state "
                f"write + TransitionMessage row in one transaction) cannot "
                f"hold across databases.",
                hint="Keep every background-bound model on the same "
                     "'default' alias as TransitionMessage — split "
                     "topologies are unsupported.",
                obj=binding.model._meta.label,
                id='django_logic.E002',
            ))
    return findings


@checks.register('django_logic')
def check_safety_net_is_scheduled(app_configs, **kwargs):
    """In celery mode, the periodic safety-net tasks must actually be in the
    running app's beat schedule (``django_logic.W002``).

    They are the durability half of ``BACKGROUND_EXECUTION='celery'``: without
    them a lost phase-2 message is never re-dispatched, an attempt that dies
    without raising is never terminalized, and completed rows are never pruned.
    Nothing else notices — a consumer ran seven weeks with them all silently
    unscheduled, accumulating 36 stranded rows, because
    ``app.conf.beat_schedule = {...}`` is ignored when the project also defines
    the ``CELERY_``-namespaced setting (Celery resolves the namespaced key
    first). Assign ``app.conf['CELERY_BEAT_SCHEDULE']`` instead.

    Matched by task name, not entry key, so renamed entries still pass.
    """
    from django.apps import apps

    # BACKGROUND_EXECUTION defaults to 'celery' and the core app registers
    # checks too, so without this an install that never added the background
    # app would get a false warning on every `manage.py check` — and fail any
    # CI running `check --fail-level WARNING`. Same gate as E002; an install
    # that *does* bind background transitions without the app is E003.
    if not apps.is_installed('django_logic.background'):
        return []

    from django_logic.background import settings as bg_settings

    if bg_settings.background_execution() != bg_settings.EXECUTION_CELERY:
        return []
    try:
        from celery import current_app
    except ImportError:  # pragma: no cover - celery is a hard dependency
        return []
    try:
        scheduled = current_app.conf.beat_schedule or {}
    except Exception:
        # No usable Celery configuration in this process (a management
        # command in a project that configures Celery lazily). Not our
        # place to fail the check run over it.
        return []

    shipped = {entry['task'] for entry in bg_settings.beat_schedule().values()}
    present = {
        entry.get('task') for entry in scheduled.values()
        if isinstance(entry, dict)
    }
    missing = sorted(shipped - present)
    if not missing:
        return []
    return [checks.Warning(
        "BACKGROUND_EXECUTION='celery' but these periodic safety-net tasks "
        "are not in the Celery beat schedule: %s. Lost phase-2 messages will "
        "never be re-dispatched and completed rows will never be pruned."
        % ', '.join(missing),
        hint="Install them with "
             "app.conf['CELERY_BEAT_SCHEDULE'] = "
             "{**(app.conf.beat_schedule or {}), **beat_schedule()} — note the "
             "CELERY_-namespaced key: a plain app.conf.beat_schedule "
             "assignment is silently ignored when the project defines "
             "CELERY_BEAT_SCHEDULE in Django settings. If you schedule them "
             "elsewhere (django-celery-beat's database scheduler, an external "
             "cron), silence this with "
             "SILENCED_SYSTEM_CHECKS = ['django_logic.W002'].",
        id='django_logic.W002',
    )]


#: Settings removed in 0.10.0, mapped to what to do instead. ``DJANGO_LOGIC``
#: has no unknown-key rejection, so a leftover key is silently ignored — and
#: for the two redaction knobs that means an upgrade quietly starts logging
#: kwargs a deployment had deliberately scrubbed.
_REMOVED_SETTINGS = {
    'LOG_KWARGS':
        'kwargs are always attached to log records now; scrub them with a '
        'logging.Filter on the "django-logic.transition" logger',
    'LOG_KWARGS_REDACTOR':
        'kwargs are always attached to log records now; scrub them with a '
        'logging.Filter on the "django-logic.transition" logger',
    'PHASE2_STATE_GUARD':
        'the phase-2 state guard always enforces; there are no modes',
    'SENTRY_TRANSACTION_NAMING':
        'Sentry transactions are always named per transition',
    'PROCESS_CLASS_ALIASES':
        'drain in-flight rows before renaming a Process class',
}


@checks.register('django_logic')
def check_no_removed_settings(app_configs, **kwargs):
    """Report ``DJANGO_LOGIC`` keys that 0.10.0 removed (``django_logic.W003``).

    Without this the removals fail *open* and silently: the sharpest case is a
    deployment that set ``LOG_KWARGS_REDACTOR`` for PII compliance, upgrades,
    and starts writing raw kwargs to its logs with no signal anywhere.
    """
    from django.conf import settings

    conf = getattr(settings, 'DJANGO_LOGIC', None) or {}
    if not isinstance(conf, dict):
        return []
    return [
        checks.Warning(
            f"DJANGO_LOGIC['{key}'] was removed in django-logic 0.10.0 and is "
            f"now ignored: {advice}.",
            hint='Delete the key from DJANGO_LOGIC.',
            id='django_logic.W003',
        )
        for key, advice in _REMOVED_SETTINGS.items() if key in conf
    ]


#: Every key the engine reads. The set is closed, so anything outside it (and
#: outside ``_REMOVED_SETTINGS``) is a typo.
_KNOWN_SETTINGS = frozenset({
    'BACKGROUND_EXECUTION',
    'DEFAULT_QUEUE',
    'STARTER_QUEUE',
    'LEGACY_EXCEPTION_BASE',
    'LOCK_TIMEOUT',
    'DEFER_UNLOCK_UNTIL_COMMIT',
    'STRICT_HOOK_SIGNATURES',
    'STRICT_KWARGS_SERIALIZATION',
    'TRANSITION_COVERAGE_LOG',
    'TRANSITION_MESSAGE_MAX_ERRORS',
    'TRANSITION_MESSAGE_RETRY_MINUTES',
    'TRANSITION_MESSAGE_CLEANUP_DAYS',
})


@checks.register('django_logic')
def check_no_unknown_settings(app_configs, **kwargs):
    """Report ``DJANGO_LOGIC`` keys the engine never reads
    (``django_logic.W004``, #182).

    ``DJANGO_LOGIC`` is a plain dict with no schema, so a typo —
    ``TRANSITION_MESSAGE_MAX_ERROR``, ``LOCK_TIMOUT`` — is silently ignored
    and the default silently applies. That is the failure mode behind every
    "I set the retry limit and it did nothing" report. The known set is
    closed and small, so reporting the complement is cheap and precise.

    Removed keys are excluded here because ``W003`` already names them with
    migration advice; flagging them twice would just be noise.
    """
    from django.conf import settings

    conf = getattr(settings, 'DJANGO_LOGIC', None) or {}
    if not isinstance(conf, dict):
        return []
    unknown = sorted(
        set(conf) - _KNOWN_SETTINGS - set(_REMOVED_SETTINGS)
    )
    if not unknown:
        return []
    return [checks.Warning(
        f"DJANGO_LOGIC contains {'a key' if len(unknown) == 1 else 'keys'} "
        f"django-logic does not read: {', '.join(repr(k) for k in unknown)}. "
        f"The value has no effect and the documented default applies.",
        hint=f"Check for a typo against the documented settings: "
             f"{', '.join(sorted(_KNOWN_SETTINGS))}. If you keep unrelated "
             f"keys in DJANGO_LOGIC on purpose, silence this with "
             f"SILENCED_SYSTEM_CHECKS = ['django_logic.W004'].",
        id='django_logic.W004',
    )]
