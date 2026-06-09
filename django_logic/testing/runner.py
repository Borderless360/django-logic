"""Synchronous execution helpers — run background transitions and their
retries inline, without Celery.

Built on the library's own ``sync_execution()`` context manager (which forces
phase 2 to run in-process) so tests exercise the *real* phase-1 + phase-2 code,
not a reimplementation.
"""
from __future__ import annotations


def transitions_for(process_class, action_name) -> list:
    """All class-level ``Transition`` objects named ``action_name`` reachable
    from ``process_class`` (including nested processes). Usually one."""
    found = []
    seen = set()

    def walk(cls):
        if id(cls) in seen:
            return
        seen.add(id(cls))
        for t in getattr(cls, 'transitions', None) or []:
            if t.action_name == action_name:
                found.append(t)
        for sub in getattr(cls, 'nested_processes', None) or []:
            walk(sub)

    walk(process_class)
    return found


def run_sync(instance, process_name, action_name, kwargs):
    """Drive a (synchronous) transition through the normal process entrypoint."""
    process = getattr(instance, process_name)
    return getattr(process, action_name)(**kwargs)


def run_background_sync(instance, process_name, action_name, kwargs):
    """Run a BackgroundTransition's phase 1 + phase 2 inline (no broker)."""
    from django_logic.background import sync_execution
    with sync_execution():
        process = getattr(instance, process_name)
        return getattr(process, action_name)(**kwargs)


def uncompleted_message(instance):
    """The instance's uncompleted ``TransitionMessage`` (what the periodic
    starter would re-dispatch), or ``None``."""
    from django_logic.background.models import TransitionMessage
    return (
        TransitionMessage.objects
        .filter(
            app_label=instance._meta.app_label,
            model_name=instance._meta.model_name,
            instance_id=str(instance.pk),
            is_completed=False,
        )
        .order_by('-id')
        .first()
    )


def latest_message(instance):
    """The instance's most recent ``TransitionMessage`` (completed or not)."""
    from django_logic.background.models import TransitionMessage
    return (
        TransitionMessage.objects
        .filter(
            app_label=instance._meta.app_label,
            model_name=instance._meta.model_name,
            instance_id=str(instance.pk),
        )
        .order_by('-id')
        .first()
    )


def rerun_message(message_id):
    """Re-run a specific TransitionMessage inline — what the periodic starter
    does, but synchronous and immediate (ignores the recency guard)."""
    from django_logic.background import sync_execution
    from django_logic.background.runner import run_background_transition
    with sync_execution():
        run_background_transition(message_id)
