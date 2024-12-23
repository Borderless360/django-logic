from abc import ABC

from django_logic.commands import SideEffects, Callbacks, Permissions, Conditions, NextTransition
from django_logic.constants import LogType
from django_logic.exceptions import TransitionNotAllowed
from django_logic.logger import get_logger
from django_logic.state import State


class BaseTransition(ABC):
    """
    Abstract class of any type of transition with all required methods
    """
    side_effects_class = SideEffects
    callbacks_class = Callbacks
    failure_callbacks_class = Callbacks
    permissions_class = Permissions
    conditions_class = Conditions
    next_transition_class = NextTransition

    def is_valid(self, state: State, user=None) -> bool:
        raise NotImplementedError

    def change_state(self, state: State, **kwargs):
        raise NotImplementedError

    def complete_transition(self, state: State, **kwargs):
        raise NotImplementedError

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        raise NotImplementedError


class Transition(BaseTransition):
    """
    Transition is a class which changes the state from source to target if conditions and permissions are satisfied.
    The transition could be called by action name through a related process or as a dedicated instance.

    Once executed, it validates whether or not the state is not locked at the moment,
    then it makes sure if the conditions and permissions are satisfied. Once it's done,
    it locks the instance state field, changes it to the in-progress value, and
    start running the provided list of side-effect functions. If succeed without exception,
    it unlocks the state field and changes it to target state, then runs the callback functions.
    Otherwise if an exception has been raised it start executing `failure callbacks` and
    changes the state to the failed one and unlocks the state field.
    """

    def __init__(self, action_name: str, sources: list, target: str, **kwargs):
        """
        Init of the transition.
        :param action_name: callable action name which used in a process
        :param sources: a list of source states which could be triggered the transition from.
        :param in_progress_state: a state which it will set before the side-effects executed
        :param target: a state which will be set to, after the side-effects executed.
        :param failed_state: a state which will be set to, if the side-effects raise an exception
        :param side_effects: a list of functions which will be run one before it changes to the target state
        :param failure_callbacks: a list of functions which will be run if any of side-effects raise an exception
        :param callbacks: a list of functions which will be run after the target state changed
        :param permissions: a list of functions with accepted user instance which
         define permissions of the transition
        :param conditions: a list of functions which define conditions of the transition.
        """
        self.action_name = action_name
        self.target = target
        self.sources = sources
        self.in_progress_state = kwargs.get('in_progress_state')
        self.failed_state = kwargs.get('failed_state')
        self.failure_callbacks = self.failure_callbacks_class(kwargs.get('failure_callbacks', []), transition=self)
        self.side_effects = self.side_effects_class(kwargs.get('side_effects', []), transition=self)
        self.callbacks = self.callbacks_class(kwargs.get('callbacks', []), transition=self)
        self.permissions = self.permissions_class(kwargs.get('permissions', []), transition=self)
        self.conditions = self.conditions_class(kwargs.get('conditions', []), transition=self)
        self.next_transition = self.next_transition_class(kwargs.get('next_transition', None))
        self.logger = get_logger(module_name=__name__)

    def __str__(self):
        return f"Transition: {self.action_name} to {self.target}"

    def is_valid(self, state: State, user=None) -> bool:
        """
        It validates this process to meet conditions and pass permissions
        :param state: State object
        :param user: any object used to pass permissions
        :return: True or False
        """
        return (not state.is_locked() and
                self.permissions.execute(state, user) and
                self.conditions.execute(state))

    def change_state(self, state: State, **kwargs):
        """
        This method changes a state by the following algorithm:
        - Lock state
        - Change state to `in progress` if such exists
        - Run side effects which should run `complete_transition` in case of success
        or `fail_transition` in case of failure.
        :param state: State object
        """
        if state.is_locked():
            self.logger.info(f'{state.instance_key} is locked',
                             log_type=LogType.TRANSITION_DEBUG,
                             log_data=state.get_log_data())
            raise TransitionNotAllowed("State is locked")

        if not state.lock():
            # in case of race conditions
            raise TransitionNotAllowed("State is locked")

        self.logger.info(f'{state.instance_key} has been locked',
                         log_type=LogType.TRANSITION_DEBUG,
                         log_data=state.get_log_data())
        if self.in_progress_state:
            state.set_state(self.in_progress_state)
            log_data = state.get_log_data().update({'user': kwargs.get('user', None)})
            self.logger.info(f'{state.instance_key} state changed to {self.in_progress_state}',
                             log_type=LogType.TRANSITION_DEBUG,
                             log_data=log_data)

        self._init_transition_context(kwargs)
        self.side_effects.execute(state, **kwargs)

    def complete_transition(self, state: State, **kwargs):
        """
        It completes the transition process for provided state and runs callbacks.
        The instance will be unlocked and callbacks executed
        :param state: State object
        """
        state.set_state(self.target)
        log_data = state.get_log_data()
        log_data.update({'user': kwargs.get('user', None)})
        self.logger.info(f'{state.instance_key} state changed to {self.target}',
                         log_type=LogType.TRANSITION_COMPLETED,
                         log_data=log_data)
        state.unlock()
        self.logger.info(f'{state.instance_key} has been unlocked',
                         log_type=LogType.TRANSITION_DEBUG,
                         log_data=state.get_log_data())
        self.callbacks.execute(state, **kwargs)
        self.next_transition.execute(state, **kwargs)

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        """
        It triggers fail transition in case of any failure during the side effects execution.
        :param state: State object
        :param exception: Exception that caused transition failure
        """
        if self.failed_state:
            state.set_state(self.failed_state)
            log_data = state.get_log_data()
            log_data.update({'user': kwargs.get('user', None)})
            self.logger.info(f'{state.instance_key} state changed to {self.failed_state}',
                             log_type=LogType.TRANSITION_FAILED,
                             log_data=log_data)
        state.unlock()
        self.logger.info(f'{state.instance_key} has been unlocked',
                         log_type=LogType.TRANSITION_DEBUG,
                         log_data=state.get_log_data())
        self.failure_callbacks.execute(state, exception=exception, **kwargs)

    @staticmethod
    def _init_transition_context(kwargs: dict) -> None:
        if 'context' not in kwargs:
            kwargs['context'] = {}


class Action(Transition):
    """
    Action, in contrast with Transition class, does not change the state during the normal execution.
    However, it allows to change the state to the failed one in case if such behaviour needed.

    Once executed, it validates whether or not the state is not locked at the moment,
    then it makes sure if the conditions and permissions are satisfied. Once it's done,
    it start running the provided list of side-effect functions. If succeed without exception,
    it runs the callback functions.
    Otherwise if an exception has been raised it start executing `failure callbacks` and
    changes the state to the failed one and unlocks the state field.
    """
    def __init__(self, action_name: str,  sources: list, **kwargs):
        super().__init__(action_name=action_name, sources=sources,  target='', **kwargs)

    def __str__(self):
        return f"Action: {self.action_name}"

    def change_state(self, state: State, **kwargs):
        """
        it run side effects which should run `complete_transition` in case of success
        or `fail_transition` in case of failure.
        :param state: State object
        """
        self._init_transition_context(kwargs)
        self.side_effects.execute(state, **kwargs)

    def complete_transition(self, state: State, **kwargs):
        """
        It completes the action for provided state and runs callbacks.
        :param state: State object
        """
        self.callbacks.execute(state, **kwargs)
