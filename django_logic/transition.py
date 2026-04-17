"""Transition — a single state-machine edge.

A ``Transition`` moves an instance from one of its source states to its
target state, running side-effects on success and either callbacks or
failure callbacks on completion.

``Action`` is a transition that does not change state on success but
still runs side-effects and can set a ``failed_state`` on failure.

For background-executed transitions, see
``django_logic.background.BackgroundTransition``.
"""
from abc import ABC
from uuid import UUID

from django_logic.commands import (
    Callbacks,
    Conditions,
    FailureSideEffects,
    NextTransition,
    Permissions,
    SideEffects,
)
from django_logic.exceptions import TransitionNotAllowed
from django_logic.logger import transition_logger, TransitionEventType
from django_logic.state import State


class BaseTransition(ABC):
    side_effects_class = SideEffects
    callbacks_class = Callbacks
    failure_callbacks_class = Callbacks
    failure_side_effects_class = FailureSideEffects
    permissions_class = Permissions
    conditions_class = Conditions
    next_transition_class = NextTransition

    def is_valid(self, instance, user=None) -> bool:
        raise NotImplementedError

    def change_state(self, state: State, **kwargs):
        raise NotImplementedError

    def complete_transition(self, state: State, **kwargs):
        raise NotImplementedError

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        raise NotImplementedError


class Transition(BaseTransition):
    """Synchronous transition from a source state to a target state.

    Execution order on success:
      1. lock state
      2. optionally set ``in_progress_state``
      3. run side-effects
      4. on success: set ``target``, unlock, run callbacks, run ``next_transition``
      5. on failure: run ``failure_side_effects``, set ``failed_state``,
         unlock, run ``failure_callbacks`` (and re-raise)
    """

    def __init__(self, action_name: str, sources: list, target: str, **kwargs):
        self.action_name = action_name
        self.target = target
        self.sources = list(sources)
        self.in_progress_state = kwargs.get('in_progress_state')
        if self.in_progress_state and self.in_progress_state not in self.sources:
            self.sources.append(self.in_progress_state)
        self.failed_state = kwargs.get('failed_state')
        self.failure_callbacks = self.failure_callbacks_class(
            kwargs.get('failure_callbacks', []), transition=self
        )
        self.failure_side_effects = self.failure_side_effects_class(
            kwargs.get('failure_side_effects', []), transition=self
        )
        self.side_effects = self.side_effects_class(
            kwargs.get('side_effects', []), transition=self
        )
        self.callbacks = self.callbacks_class(
            kwargs.get('callbacks', []), transition=self
        )
        self.permissions = self.permissions_class(
            kwargs.get('permissions', []), transition=self
        )
        self.conditions = self.conditions_class(
            kwargs.get('conditions', []), transition=self
        )
        self.next_transition = self.next_transition_class(
            kwargs.get('next_transition')
        )

    def __str__(self):
        return f"Transition: {self.action_name} to {self.target}"

    def __repr__(self):
        return self.__str__()

    def is_valid(self, instance, user=None) -> bool:
        return (
            self.permissions.execute(instance, user)
            and self.conditions.execute(instance)
        )

    def change_state(self, state: State, **kwargs) -> UUID | None:
        process_class = kwargs.get('process_class', '')
        process_class_name = process_class.split('.')[-1] if process_class else ''
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.START.value} '
            f'{process_class_name} {self.action_name} {state.instance_key} '
            f'{kwargs.get("root_id")} {kwargs.get("parent_id")}',
            extra={'kwargs': kwargs, 'state_hash': state._get_hash()},
        )

        if state.is_locked() or not state.lock():
            raise TransitionNotAllowed("State is locked")

        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.LOCK.value}'
        )

        if self.in_progress_state:
            state.set_state(self.in_progress_state)
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
                f'{self.in_progress_state}'
            )

        self._init_transition_context(kwargs)
        self.side_effects.execute(state, **kwargs)
        return kwargs.get('tr_id')

    def complete_transition(self, state: State, **kwargs):
        """Write target state, unlock, then run callbacks.

        The lock is released **before** callbacks run, so a callback can
        safely trigger another transition on the same instance. If the
        worker crashes during callbacks they are lost — callbacks are
        best-effort.
        """
        state.set_state(self.target)
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
            f'{self.target}'
        )

        state.unlock()
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.UNLOCK.value}'
        )

        self.callbacks.execute(state, **kwargs)
        self.next_transition.execute(state, **kwargs)

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        if self.failed_state:
            state.set_state(self.failed_state)
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
                f'{self.failed_state}'
            )

        self.failure_side_effects.execute(state, exception=exception, **kwargs)

        state.unlock()
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.UNLOCK.value}'
        )

        self.failure_callbacks.execute(state, exception=exception, **kwargs)

    @staticmethod
    def _init_transition_context(kwargs: dict) -> None:
        kwargs.setdefault('context', {})

    def get_task_kwargs(self, state: State, **kwargs) -> dict:
        """Serialize enough context for the transition to be restored
        in a worker process. Used by ``BackgroundTransition``.
        """
        task_kwargs = {
            'app_label': state.instance._meta.app_label,
            'model_name': state.instance._meta.model_name,
            'instance_id': state.instance.pk,
            'action_name': self.action_name,
            'target': self.target,
            'process_name': state.process_name,
            'field_name': state.field_name,
            'process_class': kwargs.get('process_class'),
        }
        if 'user_id' in kwargs:
            task_kwargs['user_id'] = kwargs['user_id']
        elif (user := kwargs.get('user')) is not None:
            task_kwargs['user_id'] = user.id

        for key in ('tr_id', 'root_id', 'parent_id'):
            if key in kwargs:
                task_kwargs[key] = str(kwargs[key]) if kwargs[key] else None

        return task_kwargs


class Action(Transition):
    """Transition that does not change state on success.

    Still runs side-effects and callbacks. ``failed_state`` (if set)
    is applied on failure.
    """

    def __init__(self, action_name: str, sources: list, **kwargs):
        super().__init__(action_name=action_name, sources=sources, target='', **kwargs)

    def __str__(self):
        return f"Action: {self.action_name}"

    def change_state(self, state: State, **kwargs) -> UUID | None:
        self._init_transition_context(kwargs)
        self.side_effects.execute(state, **kwargs)
        return kwargs.get('tr_id')

    def complete_transition(self, state: State, **kwargs):
        self.callbacks.execute(state, **kwargs)
