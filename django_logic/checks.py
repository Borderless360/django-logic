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
    collect_ambiguous_in_progress_states,
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


@checks.register('django_logic')
def check_unambiguous_in_progress_ownership(app_configs, **kwargs):
    """Two bound machines must not claim the same in_progress_state on
    one (model, state_field) — a record-less stranded instance there has
    no provenance, so automatic recovery could run the wrong transition's
    failed_state and failure hooks (``django_logic.E001``).
    ``recover_stranded_states`` also skips such states at runtime, but
    the topology itself is the defect; fail loudly at check time."""
    findings = []
    for key, owners in sorted(collect_ambiguous_in_progress_states().items()):
        model_label, state_field, in_progress = key
        claimants = ', '.join(sorted(
            f'{process_cls.__name__}.{transition.action_name}'
            for process_cls, transition in owners
        ))
        findings.append(checks.Error(
            f"in_progress_state {in_progress!r} on {model_label}."
            f"{state_field} is claimed by more than one bound machine: "
            f"{claimants}.",
            hint='Give each machine a distinct in_progress_state (or bind '
                 'the processes to distinct state fields). Stranded-state '
                 'recovery skips ambiguous states, leaving instances '
                 'parked until fixed manually.',
            obj=f'{model_label}.{state_field}',
            id='django_logic.E001',
        ))
    return findings


def _process_tree_has_background_transition(process_class) -> bool:
    """Does ``process_class`` (or any process nested under it) declare a
    background transition? Duck-typed via ``is_background`` so the check
    never imports the background package for the walk itself."""
    stack, seen = [process_class], set()
    while stack:
        process_cls = stack.pop()
        if process_cls in seen:
            continue
        seen.add(process_cls)
        for transition in process_cls.transitions:
            if getattr(transition, 'is_background', False):
                return True
        stack.extend(process_cls.nested_processes or [])
    return False


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
    """
    from django.apps import apps

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
