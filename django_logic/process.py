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

    def __init__(self, state_field: str, user=None):
        """
        :param state_field:
        :param user:
        """
        self.state_field = state_field
        self.user = user

    def __get__(self, instance, owner):
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

    def get_available_transitions(self):
        # if not self.validate():
        #     process validation
            # return

        for transition in self.transitions:
            # transition validation
            if getattr(self.instance, self.state_field) in transition.sources:
                yield transition
        # TODO:
        # for sub_process in self.nested_processes:
        #     for transition in sub_process( .get_available_transitions():
        #         yield transition


class ProcessManager:
    @classmethod
    def bind_state_fields(cls, **kwargs):
        parameters = {}
        for state_field, process_class in kwargs.items():
            if not issubclass(process_class, Process):
                raise TypeError('Must be a sub class of Process')
            process_name = '{}_process'.format(state_field)
            # it creates a property function with provided instance to the Process class
            # TODO: how to pass user to the process?
            parameters[process_name] = process_class(state_field)
        parameters['state_fields'] = kwargs.keys()  # TODO: move to Meta
        return type('Process', (cls, ), parameters)

    @property
    def non_state_fields(self):
        """
        Returns list of object's non-FSM fields (idea taken from ConcurrentTransitionMixin).
        """
        # TODO: check this as it looks a not 100% correct and compare how Django does it
        field_names = set()
        for field in self._meta.fields:
            # TODO: compare the field name with the state field name
            if not field.primary_key:
                field_names.add(field.name)

                if field.name != field.attname:
                    field_names.add(field.attname)
        return field_names

    def save(self, *args, **kwargs):
        """
        It saves all objects non-FSM fields by default.
        FSM field can be saved if explicitly passed in 'update_fields' kwarg.
        """
        if self.id is not None and 'update_fields' not in kwargs:
            kwargs['update_fields'] = self.non_state_fields
        super().save(*args, **kwargs)
