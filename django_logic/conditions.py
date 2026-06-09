"""Reusable transition conditions.

Condition factories for the parent/child coordination pattern (see
``docs/recipes/nested-processes.md``): a parent transition that should only
fire once its children reach certain states. Use them as guards so the
completion check is declarative and the parent never inspects exceptions.

    from django_logic import Transition
    from django_logic.conditions import all_related_in, any_related_in

    Transition('mark_fulfilled', sources=['fulfilling'], target='fulfilled',
               conditions=[all_related_in('fulfillments', 'status', {'fulfilled'})])
    Transition('mark_action_required', sources=['fulfilling'], target='action_required',
               conditions=[all_related_in('fulfillments', 'status', {'fulfilled', 'failed'}),
                           any_related_in('fulfillments', 'status', {'failed'})])
"""
from __future__ import annotations

from collections.abc import Iterable


def all_related_in(relation: str, field: str, states: Iterable[str]):
    """Condition: the related set ``relation`` is non-empty and every member's
    ``field`` is in ``states``. (e.g. all children terminal.)"""
    wanted = set(states)

    def condition(instance, **kwargs) -> bool:
        manager = getattr(instance, relation)
        total = manager.count()
        if total == 0:
            return False
        return manager.filter(**{f'{field}__in': wanted}).count() == total

    condition.__name__ = f'all_related_in__{relation}__{field}'
    return condition


def any_related_in(relation: str, field: str, states: Iterable[str]):
    """Condition: at least one member of ``relation`` has ``field`` in
    ``states``. (e.g. any child failed.)"""
    wanted = set(states)

    def condition(instance, **kwargs) -> bool:
        return getattr(instance, relation).filter(**{f'{field}__in': wanted}).exists()

    condition.__name__ = f'any_related_in__{relation}__{field}'
    return condition
