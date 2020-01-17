import logging
from functools import partial

from django_logic.commands import Conditions, Permissions
from django_logic.exceptions import ManyTransitions
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
    state_class = State
    process_name = 'process'
    queryset = None  # It should be set up once for the main process

    def __init__(self, field_name='', instance=None, state=None):
        """
        :param field_name:
        :param instance:
        """
        self.field_name = field_name
        self.instance = instance

        if field_name is '' or instance is None:
            assert state is not None
            self.state = state
        elif state is None:
            assert field_name and instance is not None
            self.state = self.state_class(queryset=self.queryset, instance=instance, field_name=field_name)
        else:
            raise AttributeError('Process class requires either state field name and instance or state object')

    def __getattr__(self, item):
        transitions = list(self.get_available_transitions(action_name=item))

        if len(transitions) == 1:
            return partial(transitions[0].change_state, self.state)

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
        return (permissions.execute(self.state, user) and
                conditions.execute(self.state))

    def get_available_transitions(self, user=None, action_name=None):
        """
        It returns all available transition which meet conditions and pass permissions.
        Including nested processes.
        :param user: any object which used to validate permissions
        :param action_name: str
        :return: yield `django_logic.Transition`
        """
        if not self.is_valid(user):
            return

        for transition in self.transitions:
            if action_name is not None and transition.action_name != action_name:
                continue

            if self.state.cached_state in transition.sources and transition.is_valid(self.state, user):
                yield transition

        for sub_process_class in self.nested_processes:
            sub_process = sub_process_class(state=self.state)
            yield from sub_process.get_available_transitions(user=user, action_name=action_name)


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
            if not field.primary_key and field.name not in self.state_fields:
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
