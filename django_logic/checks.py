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
