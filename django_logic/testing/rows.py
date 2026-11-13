"""Stand up an uncompleted ``TransitionMessage`` row for a test.

Tests that pin behaviour around the one-uncompleted-row gate need a row
whose fields agree with the engine's keying. Hand-rolled rows get one of
the eight fields wrong and pin nothing, so this helper writes them all
from the instance itself.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone


def open_transition_message(
    instance,
    process_name: str = 'process',
    transition_name: str = 'transition',
    *,
    field_name: str = '',
    queue_name: str | None = None,
    started_minutes_ago: int | None = None,
    errors_count: int = 0,
):
    """Create and return an uncompleted ``TransitionMessage`` for ``instance``.

    The row is keyed exactly as enqueue keys it, so the sync gate, the
    enqueue constraint, and ``retry_status`` all see it. Its saved process and
    declaring class come from the bound process and named background declaration,
    so the worker can restore it without guessing. With
    ``started_minutes_ago`` the row reads as an attempt that started that
    long ago — ``started_at`` and ``modified`` both move back, so the
    retry-window classification answers for that age.
    """
    from django_logic import Process
    from django_logic.background.models import TransitionMessage
    from django_logic.process import _iter_process_tree

    process = getattr(instance, process_name)
    if not isinstance(process, Process):
        raise ValueError(f'{process_name!r} must be a bound Process.')
    matches = [
        (process_class, transition)
        for process_class in _iter_process_tree(type(process))
        for transition in process_class.transitions
        if transition.is_background and transition.action_name == transition_name
    ]
    if len(matches) != 1:
        raise ValueError(
            'open_transition_message requires exactly one declared background '
            f'transition named {transition_name!r} in {type(process).__name__}. '
            'Submit through the process to test condition-selected transitions.'
        )
    owner, transition = matches[0]
    if field_name and field_name != process.state.field_name:
        raise ValueError('field_name must match the bound process state field.')
    process_path = f'{type(process).__module__}.{type(process).__name__}'
    owner_path = f'{owner.__module__}.{owner.__name__}'
    row = TransitionMessage.objects.create(
        **TransitionMessage.instance_key(instance, process_name),
        proxy_model_label=TransitionMessage.proxy_label_for(instance),
        field_name=process.state.field_name,
        transition_name=transition_name,
        owning_process_class=owner_path,
        timeout_seconds=transition.timeout,
        queue_name=queue_name or transition.get_queue_name(),
        errors_count=errors_count,
        kwargs={'process_class': process_path, 'owning_process_class': owner_path},
    )
    if started_minutes_ago is not None:
        past = timezone.now() - timedelta(minutes=started_minutes_ago)
        # .update() bypasses auto_now, which a .save() would reset to now.
        TransitionMessage.objects.filter(pk=row.pk).update(
            started_at=past, created=past, modified=past,
        )
        row.refresh_from_db()
    return row
