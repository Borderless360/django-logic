from django_logic.commands import SideEffects, Callbacks, Permissions, Conditions
from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State


class Transition(object):
    """
    Transition could be defined as a class or as an object and used as an object
    - action name
    - transitions name
    - target
    - it changes the the state of the object from source to target by triggering available action via transition name
    - validation if the action is available throughout permissions and conditions
    - run side effects and call backs
    """
    side_effects = SideEffects()
    callbacks = Callbacks()
    permissions = Permissions()
    conditions = Conditions()
    state = State()

    def __init__(self, action_name, sources, target, **kwargs):
        self.action_name = action_name
        self.target = target
        self.sources = sources
        self.in_progress_state = kwargs.get('in_progress_state')
        self.failed_state = kwargs.get('failed_state')
        self.failure_handler = kwargs.get('failure_handler')
        self.side_effects = kwargs.get('side_effects', [])
        self.callbacks = kwargs.get('callbacks', [])
        self.permissions = kwargs.get('permissions', [])
        self.conditions = kwargs.get('conditions', [])

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

    def change_state(self, instance, field_name):
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
        self.side_effects.execute(instance, field_name)

    def complete_transition(self, instance, field_name):
        """
        It completes the transition process for provided instance and filed name.
        The instance will be unlocked and callbacks exc
        :param instance:
        :param field_name:
        :return:
        """
        self.state.set_state(instance, field_name, self.target)
        self.state.unlock(instance, field_name)
        self.callbacks.execute(instance, field_name)
    
    def fail_transition(self, instance, field_name):
        """
        It triggers fail transition in case of any failure during the side effects execution.
        :param instance: any
        :param field_name: str
        """
        if self.failed_state:
            self.state.set_state(instance, field_name, self.failed_state)
        self.state.unlock(instance, field_name)
