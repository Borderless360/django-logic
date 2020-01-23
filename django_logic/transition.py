import logging

from django_logic.commands import SideEffects, Callbacks, Permissions, Conditions
from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State


class Transition(object):
    """
    Transition could be defined as a class and used as an object
    - action name
    - transitions name
    - target
    - it changes the the state of the object from source to target by triggering available action via transition name
    - validation if the action is available throughout permissions and conditions
    - run side effects and call backs
    """
    side_effects_class = SideEffects
    callbacks_class = Callbacks
    failure_callbacks_class = Callbacks
    permissions_class = Permissions
    conditions_class = Conditions

    def __init__(self, action_name: str, sources: list, target: str, **kwargs):
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

    def __str__(self):
        return "Transition: {} to {}".format(self.action_name, self.target)

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
            logging.info(f'{state.instance_key} is locked')
            raise TransitionNotAllowed("State is locked")

        state.lock()
        logging.info(f'{state.instance_key} has been locked')
        if self.in_progress_state:
            state.set_state(self.in_progress_state)
            logging.info(f'{state.instance_key} state changed to {self.in_progress_state}')
        self.side_effects.execute(state, **kwargs)

    def complete_transition(self, state: State, **kwargs):
        """
        It completes the transition process for provided state.
        The instance will be unlocked and callbacks executed
        :param state: State object
        """
        state.set_state(self.target)
        logging.info(f'{state.instance_key} state changed to {self.target}')
        state.unlock()
        logging.info(f'{state.instance_key} has been unlocked')
        self.callbacks.execute(state, **kwargs)

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        """
        It triggers fail transition in case of any failure during the side effects execution.
        :param state: State object
        :param exception: Exception that caused transition failure
        """
        if self.failed_state:
            state.set_state(self.failed_state)
            logging.info(f'{state.instance_key} state changed to {self.failed_state}')
        state.unlock()
        logging.info(f'{state.instance_key} has been unlocked')
        self.failure_callbacks.execute(state, exception=exception, **kwargs)
