"""Synchronous execution helpers — run background transitions and their
retries inline, without Celery.

Built on the library's own ``sync_execution()`` context manager (which forces
the worker to run in-process) so tests exercise the *real* enqueue + worker code,
not a reimplementation.
"""
from __future__ import annotations


def transitions_for(process_class, action_name) -> list:
    """All class-level ``Transition`` objects named ``action_name`` reachable
    from ``process_class`` (including nested processes). Usually one."""
    return [
        t for t in all_transitions(process_class)
        if t.action_name == action_name
    ]


def all_transitions(process_class) -> list:
    """Every class-level ``Transition`` reachable from ``process_class``
    (including nested processes) — the full instrumentation surface for one
    drive. A drive can execute more than the named action (``next_transition``
    follow-ups, callback-triggered transitions), so tracking must cover the
    whole tree for the side-effect assertions to be truthful."""
    from django_logic.process import _iter_process_tree

    return [
        transition
        for process_cls in _iter_process_tree(process_class)
        for transition in getattr(process_cls, 'transitions', None) or []
    ]


def run_background_sync(instance, process_name, action_name, kwargs):
    """Run a BackgroundTransition's enqueue + the worker inline (no broker)."""
    from django_logic.background import sync_execution
    with sync_execution():
        process = getattr(instance, process_name)
        return getattr(process, action_name)(**kwargs)


def _messages(instance, process_name, **filters):
    """Base queryset for one bound process's ``TransitionMessage`` rows,
    newest first. Scoping is mandatory: two processes on different state
    fields of the same model are independent state machines and their rows
    must not be confused."""
    from django_logic.background.models import TransitionMessage
    return TransitionMessage.objects.filter(
        app_label=instance._meta.app_label,
        model_name=instance._meta.model_name,
        instance_id=str(instance.pk),
        process_name=process_name,
        **filters,
    ).order_by('-id')


def uncompleted_message(instance, process_name):
    """The bound process's uncompleted ``TransitionMessage`` (what a
    worker's next claim would pick up), or ``None``."""
    return _messages(instance, process_name, is_completed=False).first()


def latest_message(instance, process_name):
    """The bound process's most recent ``TransitionMessage`` (completed or
    not)."""
    return _messages(instance, process_name).first()


def message_for(instance, transition_name, process_name):
    """The bound process's most recent ``TransitionMessage`` for one action.

    Used by ``assert_transition_owner`` to pin the recorded
    ``owning_process_class`` of a specific transition in a chained/next-
    transition workflow, where several TransitionMessage rows exist for one instance.
    """
    return _messages(instance, process_name,
                     transition_name=transition_name).first()


def rerun_message(message_id):
    """Re-run a specific TransitionMessage inline — what a worker's next
    claim does, but synchronous and immediate (ignores the retry wait)."""
    from django_logic.background import sync_execution
    from django_logic.background.runner import run_background_transition
    with sync_execution():
        run_background_transition(message_id)
