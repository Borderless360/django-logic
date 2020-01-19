import logging
from functools import partial

from django_logic.commands import Conditions, Permissions
from django_logic.exceptions import TransitionNotAllowed
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
    queryset_name = 'objects'

    def __init__(self, field_name='', instance=None, state=None):
        """
        :param field_name: state or status field name
        :param instance: Model instance
        """
        self.field_name = field_name
        self.instance = instance
        if field_name is '' or instance is None:
            assert state is not None
            self.state = state
        elif state is None:
            assert field_name and instance is not None
            self.state = self.state_class(queryset_name=self.queryset_name,
                                          instance=instance,
                                          field_name=field_name,
                                          process_name=self.process_name)
        else:
            raise AttributeError('Process class requires either state field name and instance or state object')

    def __getattr__(self, item):
        return partial(self._get_transition_method, item)

    def _get_transition_method(self, action_name: str, **kwargs):
        """
        It returns a callable transition method by provided action name.
        """
        user = kwargs.pop('user') if 'user' in kwargs else None
        transitions = list(self.get_available_transitions(action_name=action_name, user=user))

        if len(transitions) == 1:
            transition = transitions[0]
            logger.info(f"{self.state.instance_key}, process {self.process_name} "
                        f"executes '{action_name}' transition from {self.state.cached_state} "
                        f"to {transition.target}")
            return transition.change_state(self.state, **kwargs)

        elif len(transitions) > 1:
            logger.error(f"Runtime error: {self.state.instance_key} has several "
                         f"transitions with action name '{action_name}'. "
                         f"Make sure to specify conditions and permissions accordingly to fix such case")
            raise TransitionNotAllowed("There are several transitions available")
        raise TransitionNotAllowed(f"Process class {self.__class__} has no transition with action name {action_name}")

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
