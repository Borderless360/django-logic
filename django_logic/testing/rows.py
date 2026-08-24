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
    enqueue constraint, and ``retry_status`` all see it. With
    ``started_minutes_ago`` the row reads as an attempt that started that
    long ago — ``started_at`` and ``modified`` both move back, so the
    retry-window classification answers for that age.
    """
    from django_logic import conf
    from django_logic.background.models import TransitionMessage

    row = TransitionMessage.objects.create(
        **TransitionMessage.instance_key(instance, process_name),
        field_name=field_name,
        transition_name=transition_name,
        queue_name=queue_name or conf.default_queue(),
        errors_count=errors_count,
        kwargs={},
    )
    if started_minutes_ago is not None:
        past = timezone.now() - timedelta(minutes=started_minutes_ago)
        # .update() bypasses auto_now, which a .save() would reset to now.
        TransitionMessage.objects.filter(pk=row.pk).update(
            started_at=past, created=past, modified=past,
        )
        row.refresh_from_db()
    return row
