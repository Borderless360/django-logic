"""BackgroundTransition / BackgroundAction — durable, queue-routed.

Phase 1 (what ``change_state`` does) is identical in both Celery and
Sync execution modes:

* validate conditions + permissions,
* atomically write ``in_progress_state`` (for ``BackgroundTransition``)
  and create a ``TransitionMessage`` row,
* hand the row id to the dispatcher, which either ``apply_async`` the
  Celery task (Celery mode) or call phase 2 inline (Sync mode).

Phase 2 lives in :mod:`django_logic.background.runner` and is shared
between both modes.
"""
from __future__ import annotations

from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction

from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from django_logic.background.serializers import serialize_kwargs
from django_logic.logger import transition_logger, TransitionEventType
from django_logic.state import State
from django_logic.transition import Transition


class BackgroundTransition(Transition):
    """State-changing transition that runs its side-effects in the background.

    Required:
        - ``queue`` — the Celery queue (and, in Sync mode, the record of
          which queue the transition *would* have used in production).
          Omitting it raises :class:`ImproperlyConfigured` at class
          creation.

    Recommended:
        - ``in_progress_state`` — if omitted, the state field does not
          change until phase 2 finishes. Providing it is strongly
          recommended so concurrent readers see "in progress" rather
          than the pre-transition state. ``in_progress_state`` values
          must be unique within a :class:`django_logic.Process`.
    """

    def __init__(
        self,
        action_name: str,
        sources: list,
        target: str,
        *,
        queue: str,
        **kwargs,
    ):
        if not queue or not isinstance(queue, str):
            raise ImproperlyConfigured(
                f"BackgroundTransition '{action_name}' requires a non-empty "
                f"'queue' string. No default queue is provided — every "
                f"background transition must declare its own."
            )
        self.queue = queue
        super().__init__(
            action_name=action_name, sources=sources, target=target, **kwargs
        )

    def change_state(self, state: State, **kwargs) -> UUID | None:
        process_class = kwargs.get('process_class', '')
        process_class_name = process_class.split('.')[-1] if process_class else ''
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.START.value} '
            f'{process_class_name} {self.action_name} {state.instance_key} '
            f'{kwargs.get("root_id")} {kwargs.get("parent_id")} '
            f'[background queue={self.queue}]',
            extra={'kwargs': kwargs, 'state_hash': state._get_hash()},
        )

        if not self.is_valid(state.instance, kwargs.get('user')):
            from django_logic.exceptions import TransitionNotAllowed
            raise TransitionNotAllowed(
                f"BackgroundTransition '{self.action_name}' rejected by "
                f"its conditions or permissions."
            )

        tm = self._phase_one_atomic(state, kwargs)

        from django_logic.background.dispatch import dispatch_transition
        dispatch_transition(tm)

        return kwargs.get('tr_id')

    def _phase_one_atomic(self, state: State, kwargs: dict) -> TransitionMessage:
        """Atomic: set in_progress_state + create TransitionMessage row.

        Raises :class:`AlreadyInProgress` if the partial unique
        constraint fires (another uncompleted TM exists for the same
        instance).
        """
        instance_lookup = {
            'app_label': state.instance._meta.app_label,
            'model_name': state.instance._meta.model_name,
            'instance_id': state.instance.pk,
        }
        try:
            serialized = serialize_kwargs(kwargs)
        except TypeError as e:
            raise ImproperlyConfigured(
                f"BackgroundTransition '{self.action_name}' received a "
                f"kwarg that is not JSON-serializable: {e}. Every value "
                f"passed to a background transition must be persistable "
                f"on the TransitionMessage row."
            ) from e

        try:
            with transaction.atomic():
                if self.in_progress_state:
                    state.set_state(self.in_progress_state)
                    transition_logger.info(
                        f'{kwargs.get("tr_id")} '
                        f'{TransitionEventType.SET_STATE.value} '
                        f'{self.in_progress_state}'
                    )
                tm = TransitionMessage.objects.create(
                    process_name=state.process_name,
                    transition_name=self.action_name,
                    queue_name=self.queue,
                    kwargs=serialized,
                    **instance_lookup,
                )
        except IntegrityError as exc:
            raise AlreadyInProgress(
                f"{state.instance_key}: another transition is already "
                f"in progress for this instance."
            ) from exc

        transition_logger.info(
            f'{kwargs.get("tr_id")} TransitionMessage#{tm.pk} created '
            f'(queue={self.queue})'
        )
        return tm


class BackgroundAction(BackgroundTransition):
    """Background-executed action — runs side-effects with no state change.

    Same durability contract as :class:`BackgroundTransition`. The only
    differences:

    * ``target`` is always empty (no state write on success),
    * ``in_progress_state`` is not meaningful and is rejected at
      construction time,
    * failure at ``MAX_ERRORS`` optionally writes ``failed_state``.
    """

    def __init__(self, action_name: str, sources: list, *, queue: str, **kwargs):
        if kwargs.get('in_progress_state'):
            raise ImproperlyConfigured(
                f"BackgroundAction '{action_name}' cannot declare "
                f"in_progress_state — actions do not change state on "
                f"success. Use BackgroundTransition if you need to mark "
                f"in-progress."
            )
        # target='' is the sentinel for "no state change".
        super().__init__(
            action_name=action_name,
            sources=sources,
            target='',
            queue=queue,
            **kwargs,
        )

    def __str__(self) -> str:
        return f"BackgroundAction: {self.action_name}"

    def complete_transition(self, state: State, **kwargs):
        # No state change. Callbacks run best-effort (see runner).
        pass
