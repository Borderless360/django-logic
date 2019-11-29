import logging
from functools import partial


from django_logic.exceptions import ManyTransitions, TransitionNotAllowed

logger = logging.getLogger(__name__)


class Process:
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
    process_name = None
    states = []
    nested_processes = []
    transitions = []
    conditions = None
    permissions = None

    def __init__(self, state_field: str, instance=None):
        """
        :param state_field:
        """
        self.state_field = state_field
        self.instance = instance

    def __get__(self, instance, owner):
        if self.instance is None:
            self.instance = instance
        return self

    def __getattr__(self, item):
        transitions = list(filter(
            lambda transition:  transition.action_name == item,
            self.get_available_transitions()
        ))

        if len(transitions) == 1:
            return partial(transitions[0].change_state,
                           instance=self.instance,
                           state_field=self.state_field)

        # This exceptions should be handled otherwise it will be very annoying
        elif transitions:
            raise ManyTransitions("There are several transitions available")
        else:
            # TODO: transition not available
            raise TransitionNotAllowed('Transition not allowed')
    
    @classmethod
    def get_process_name(cls):
        return cls.process_name or str(cls.__name__)

    def validate(self, user=None) -> bool:
        """
        It validates this process to meet conditions and pass permissions
        :param user: any object used to pass permissions
        :return: True or False
        """
        if self.permissions is not None:
            if not self.permissions.execute(self.instance, user):
                return False

        if self.conditions is not None:
            if not self.conditions.execute(self.instance):
                return False

        return True

    def get_available_transitions(self, user=(None or any)):
        """
        It returns all available transition which meet conditions and pass permissions.
        Including nested processes.
        :param user: any object which used to validate permissions
        :return: yield `django_logic.Transition`
        """
        if not self.validate(user):
            return

        for transition in self.transitions:
            state = getattr(self.instance, self.state_field)
            if state in transition.sources and transition.validate(user):
                yield transition

        for sub_process_class in self.nested_processes:
            sub_process = sub_process_class(state_field=self.state_field,
                                            instance=self.instance)
            for transition in sub_process.get_available_transitions(user):
                yield transition


class ProcessManager:
    @classmethod
    def bind_state_fields(cls, **kwargs):
        parameters = {}
        for state_field, process_class in kwargs.items():
            if not issubclass(process_class, Process):
                raise TypeError('Must be a sub class of Process')
            process_name = '{}_process'.format(state_field)
            parameters[process_name] = process_class(state_field)
        return type('Process', (cls, ), parameters)
