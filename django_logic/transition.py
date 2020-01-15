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
    state = State()

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

    def validate(self, instance: any, field_name: str, user=None) -> bool:
        """
        It validates this process to meet conditions and pass permissions
        :param field_name:
        :param instance: any instance used to meet conditions
        :param user: any object used to pass permissions
        :return: True or False
        """
        return (not self.state.is_locked(instance, field_name) and
                self.permissions.execute(instance, user) and
                self.conditions.execute(instance))

    def change_state(self, instance, field_name, **kwargs):
        """
        This method changes a state of the provided instance and file name by the following algorithm:
        - Lock state
        - Change state to `in progress` if such exists
        - Run side effects which should run `complete_transition` in case of success
        or `fail_transition` in case of failure.
        :param instance: any
        :param field_name: str
        """
        if self.state.is_locked(instance, field_name):
            raise TransitionNotAllowed("State is locked")

        self.state.lock(instance, field_name)
        if self.in_progress_state:
            self.state.set_state(instance, field_name, self.in_progress_state)
        self.side_effects.execute(instance, field_name, **kwargs)

    def complete_transition(self, instance, field_name, **kwargs):
        """
        It completes the transition process for provided instance and filed name.
        The instance will be unlocked and callbacks exc
        :param instance:
        :param field_name:
        :return:
        """
        self.state.set_state(instance, field_name, self.target)
        self.state.unlock(instance, field_name)
        self.callbacks.execute(instance, field_name, **kwargs)
    
    def fail_transition(self, instance, field_name, **kwargs):
        """
        It triggers fail transition in case of any failure during the side effects execution.
        :param instance: any
        :param field_name: str
        """
        if self.failed_state:
            self.state.set_state(instance, field_name, self.failed_state)
        self.state.unlock(instance, field_name)
        self.failure_callbacks.execute(instance, field_name, **kwargs)