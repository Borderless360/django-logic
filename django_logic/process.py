import logging
from functools import partial

from django_logic.commands import Conditions, Permissions
from django_logic.exceptions import ManyTransitions, TransitionNotAllowed
from django_logic.state import State

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
    nested_processes = []
    transitions = []
    conditions = []
    permissions = []
    conditions_class = Conditions
    permissions_class = Permissions
    process_name = 'process'

    def __init__(self, field_name: str, instance=None):
        """
        :param field_name:
        """
        self.field_name = field_name
        self.instance = instance

    def __getattr__(self, item):
        transitions = list(self.get_available_transitions(action_name=item))

        if len(transitions) == 1:
            return partial(transitions[0].change_state,
                           instance=self.instance,
                           field_name=self.field_name)

        # This exceptions should be handled otherwise it will be very annoying
        elif transitions:
            raise ManyTransitions("There are several transitions available")
        raise AttributeError(f"Process class {self.__class__} has no transition with action name {item}")

    def is_valid(self, user=None) -> bool:
        """
        It validates this process to meet conditions and pass permissions
        :param user: any object used to pass permissions
        :return: True or False
        """
        permissions = self.permissions_class(commands=self.permissions)
        conditions = self.conditions_class(commands=self.conditions)
        return (permissions.execute(self.instance, user) and
                conditions.execute(self.instance))

    def get_available_transitions(self, user=None, action_name=None):
        """
        It returns all available transition which meet conditions and pass permissions.
        Including nested processes.
        :param action_name:
        :param user: any object which used to validate permissions
        :return: yield `django_logic.Transition`
        """
        if not self.is_valid(user):
            return

        state = State().get_db_state(self.instance, self.field_name)
        for transition in self.transitions:
            if action_name is not None and transition.action_name != action_name:
                continue

            if state in transition.sources and transition.is_valid(self.instance,
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
        def make_process_getter(field_name, field_class):
            return lambda self: field_class(field_name=field_name, instance=self)

        parameters = {'state_fields': []}
        for state_field, process_class in kwargs.items():
            if not issubclass(process_class, Process):
                raise TypeError('Must be a sub class of Process')
            parameters[process_class.process_name] = property(make_process_getter(state_field, process_class))
            parameters['state_fields'].append(state_field)
        return type('Process', (cls, ), parameters)

    @property
    def non_state_fields(self):
        """
        Returns list of object's non-state fields.
        """
        field_names = set()
        for field in self._meta.fields:
            if not field.primary_key and not field.name in self.state_fields:
                field_names.add(field.name)

                if field.name != field.attname:
                    field_names.add(field.attname)
        return field_names

    def save(self, *args, **kwargs):
        """
        It saves all non-state fields by default.
        State fields can be saved if explicitly passed in 'update_fields' kwarg.
        """
        if self.id is not None and 'update_fields' not in kwargs:
            kwargs['update_fields'] = self.non_state_fields
        super().save(*args, **kwargs)
