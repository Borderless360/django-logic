"""Django system checks for django-logic.

Bind-time validation warns through the transition logger, which runs
during ``AppConfig.ready()`` — before test/dev logging is configured, so
warn-mode consumers can miss it entirely. The checks framework runs after
setup and is surfaced by ``manage.py check``, every test run, and deploy
checks, regardless of logging configuration.
"""
from django.core import checks

from django_logic.process import (
    ProcessManager, collect_hook_signature_offenders,
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
