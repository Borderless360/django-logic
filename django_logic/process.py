import logging
from functools import partial

from django.core.cache import cache
from django.db import transaction

from efsm.exceptions import ManyTransitions, TransitionNotAllowed

logger = logging.getLogger(__name__)


class BaseCommand:
    def __init__(self, commands=None):
        self.commands = commands or []

    def execute(self):
        raise NotImplementedError


class CeleryCommand(BaseCommand):
    def execute(self):
        # TODO: wrap all commands into celery task with asck late = True
        # TODO: execute them in a queue
        pass


class Command(BaseCommand):
    def execute(self):
        for command in self.commands:
            command()


class Conditions(BaseCommand):
    # TODO: support hints

    def execute(self):
        return all(command() for command in self.commands)


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

    def __init__(self, state_field, user=None):
        """

        :param instance:
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
            if getattr(self.instance, self.state_field) in transition.sources and transition.validate():
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


# def success(instance):
#     logger.info('Transition started: {app_label}.{model_name}.{method_name} '
#                 '(instance id {instance_id})'.format(
#         app_label=instance._meta.app_label,
#         model_name=instance._meta.model_name,
#         method_name=self.action_name,
#         instance_id=instance.id
#     ))
#
#     # TODO: processing state should be taken either from process or transition
#     instance._set_state(instance, state_field, instance.target)
#     instance._unlock(instance)
#     # TODO: run callbacks
#     # TODO: catch exceptions and use failure handler
#

class Transition:
    """
    Transition could be defined as a class or as an object and used as an object
    - action name
    - transitions name
    - target
    - it changes the the state of the object from source to target by triggering available action via transition name
    - validation if the action is available throughout permissions and conditions
    - run side effects and call backs
    """
    def __init__(self, action_name, target, sources, **kwargs):
        self.action_name = action_name
        self.target = target
        self.sources = sources
        self.side_effects = kwargs.get('side_effects')
        self.callbacks = kwargs.get('callbacks')
        self.failure_handler = kwargs.get('failure_handler')
        self.processing_state = kwargs.get('processing_state')
        self.permissions = kwargs.get('permissions')
        self.conditions = kwargs.get('conditions')
        self.parent_process = None  # initialised by process

    def change_state(self, instance, state_field):
        # TODO: consider adding the process as it also has side effects and callback (or remove them from it)
        # run the conditions and permissions
        # Lock state
        # run side effects
        # change state via transition to the next state
        # run callbacks
        print("ALL WORKS WITH ", instance, state_field)

        if self._is_locked(instance):
            raise TransitionNotAllowed("State is locked")

        self._lock(instance)
        # self.side_effects.add(success(self))
        try:
            self.side_effects.execute()
        except Exception as ex:
            pass
            # TODO: logger
        else:
            self._unlock(instance)


    def _get_hash(self, instance):
        return "FSM-{}-{}-{}".format(instance._meta.app_label, instance._meta.model_name, instance.pk)

    def _lock(self, instance):
        cache.set(self._get_hash(instance), True)

    def _unlock(self, instance):
        cache.delete(self._get_hash(instance))

    def _is_locked(self, instance):
        return cache.get(self._get_hash(instance)) or False

    def _get_db_state(self, instance, state_field):
        """
        Fetches state directly from db instead of model instance.
        """
        return instance._meta.model.objects.values_list(state_field, flat=True).get(pk=instance.id)

    def _set_state(self, instance, state_field, state):
        """
        Sets intermediate state to instance's field until transition is over.
        """
        self._lock(instance)
        # TODO: how would it work if it's used within another transition?
        instance._meta.model.objects.filter(pk=instance.id).update(**{state_field: state})
