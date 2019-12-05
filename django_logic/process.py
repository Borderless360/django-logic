import logging
from functools import partial

from django_logic.commands import Conditions, Permissions
from django_logic.exceptions import ManyTransitions, TransitionNotAllowed
from django_logic.utils import convert_to_snake_case, convert_to_readable_name

logger = logging.getLogger(__name__)


class Process(object):
    """
    Process should be explicitly defined as a class and used as an object.
    - process name
    - nested states
    - contains either transitions and processes
    - transitions defined as parameters of the class
    - processes should be defined in the list
    - validate - conditions and permissions of the process affects all transitions/processes inside
    - has methods like get_all_available_transitions, etc
    """
    states = []
    nested_processes = []
    transitions = []
    conditions = Conditions()
    permissions = Permissions()

    def __init__(self, field_name: str, instance=None):
        """
        :param field_name:
        """
        self.field_name = field_name
        self.instance = instance

    def __get__(self, instance, owner):
        if self.instance is None:
            self.instance = instance
        return self

    def __getattr__(self, item):
        transitions = list(self.get_available_transitions(action_name=item))

        if len(transitions) == 1:
            return partial(transitions[0].change_state,
                           instance=self.instance,
                           field_name=self.field_name)

        # This exceptions should be handled otherwise it will be very annoying
        elif transitions:
            raise ManyTransitions("There are several transitions available")
        else:
            # TODO: transition not available
            raise TransitionNotAllowed('Transition not allowed')
    
    @classmethod
    def get_process_name(cls):
        return convert_to_snake_case(str(cls.__name__))

    @classmethod
    def get_readable_name(cls):
        return convert_to_readable_name(str(cls.__name__))

    def validate(self, user=None) -> bool:
        """
        It validates this process to meet conditions and pass permissions
        :param user: any object used to pass permissions
        :return: True or False
        """
        return (self.permissions.execute(self.instance, user) and
                self.conditions.execute(self.instance))

    def get_available_transitions(self, user=None, action_name=None):
        """
        It returns all available transition which meet conditions and pass permissions.
        Including nested processes.
        :param action_name:
        :param user: any object which used to validate permissions
        :return: yield `django_logic.Transition`
        """
        if not self.validate(user):
            return

        for transition in self.transitions:
            state = getattr(self.instance, self.field_name)
            if action_name is not None and transition.action_name != action_name:
                continue

            if state in transition.sources and transition.validate(self.instance,
                                                                   self.field_name,
                                                                   user):
                yield transition

        for sub_process_class in self.nested_processes:
            sub_process = sub_process_class(instance=self.instance, field_name=self.field_name)
            for transition in sub_process.get_available_transitions(user=user,
                                                                    action_name=action_name):
                yield transition


class ProcessManager:
    @classmethod
    def bind_state_fields(cls, **kwargs):
        parameters = {}
        for state_field, process_class in kwargs.items():
            if not issubclass(process_class, Process):
                raise TypeError('Must be a sub class of Process')
            parameters[process_class.get_process_name()] = property(lambda self: process_class(
                field_name=state_field,
                instance=self))
        return type('Process', (cls, ), parameters)
