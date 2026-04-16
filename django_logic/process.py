import uuid
import warnings
from contextvars import ContextVar
from django_logic.logger import TransitionEventType
from django_logic.commands import Conditions, Permissions
from django_logic.constants import LogType
from django_logic.exceptions import TransitionNotAllowed
from django_logic.logger import logger, transition_logger
from django_logic.logger import get_logger
from django_logic.state import State

# Thread-safe per-execution-chain context that propagates transition metadata
# (root_id, tr_id) through nested callbacks without explicit **kwargs forwarding.
_transition_context: ContextVar[dict | None] = ContextVar('_transition_context', default=None)


class Process(object):
    """
    Process should be explicitly defined as a class and used as an object.
    - process name
    - nested states
    - contains either transitions and processes
    - transitions defined as parameters of the class
    - processes should be defined in the list
    - validate - conditions and permissions of the process affect all transitions/processes inside
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
        if field_name == '' or instance is None:
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
        # DEPRECATED
        self.logger = get_logger(module_name=__name__)

    def __getattr__(self, item):
        def transition_method(*args, **kwargs):
            # Strip action_name from kwargs before calling; otherwise when kwargs
            # from a parent transition (e.g. Celery task or nested side-effect)
            # include action_name, we get "multiple values for argument 'action_name'"
            # during argument binding, before _get_transition_method even runs.
            kwargs.pop('action_name', None)
            return self._get_transition_method(item, **kwargs)
        return transition_method

    def _get_transition_method(self, action_name: str, **kwargs):
        """
        It returns a callable transition method for the provided action name.
        """
        # Inherit transition context from parent (propagates root_id, parent_id
        # through nested callbacks even without explicit **kwargs forwarding)
        parent_ctx = _transition_context.get()
        if parent_ctx:
            kwargs.setdefault('root_id', parent_ctx['root_id'])
            kwargs.setdefault('tr_id', parent_ctx['tr_id'])

        user = kwargs['user'] if 'user' in kwargs else None
        transition = self.get_transition_by_action_name(action_name, user)
        # DEPRECATED
        self.logger.info(f"{self.state.instance_key}, process {self.process_name} "
                            f"executes '{action_name}' transition from {self.state.get_state()} "
                            f"to {transition.target}",
                            log_type=LogType.TRANSITION_DEBUG,
                            log_data=self.state.get_log_data())

        tr_id = uuid.uuid4()
        logger.info(
            f"{tr_id} {self.state.instance_key}, process {self.process_name} "
            f"executes '{action_name}' transition from {self.state.get_state()} "
            f"to {transition.target}  "
        )
        kwargs['root_id'] = kwargs.get('root_id', tr_id)
        kwargs['parent_id'] = kwargs.get('tr_id', tr_id)
        kwargs['tr_id'] = tr_id
        # Pass process class for cases where process is not bound to model
        if 'process_class' not in kwargs:
            process_class = f"{self.__class__.__module__}.{self.__class__.__name__}"
            kwargs['process_class'] = process_class

        # Set context so nested transitions (from callbacks) can inherit root_id/parent_id
        token = _transition_context.set({
            'root_id': kwargs['root_id'],
            'tr_id': kwargs['tr_id'],
        })
        try:
            # Only catch exceptions at the top level (root_id == tr_id means this is the root transition)
            # Nested transitions should propagate exceptions to their parents
            is_root = kwargs.get('root_id') == tr_id
            if is_root:
                try:
                    return transition.change_state(self.state, **kwargs)
                except Exception as e:
                    transition_logger.error(
                        f"{tr_id} {TransitionEventType.FAIL.value}: {type(e).__name__}: {e}",
                        exc_info=True
                    )
                    # Do not re-raise the exception, just return the tr_id
                    # We need this for backward compatibility with the old code for now
                    return tr_id
            else:
                return transition.change_state(self.state, **kwargs)
        finally:
            _transition_context.reset(token)

    def is_valid(self, user=None) -> bool:
        """
        It validates this process to meet conditions and pass permissions
        :param user: any object used to pass permissions
        :return: True or False
        """
        permissions = self.permissions_class(commands=self.permissions)
        conditions = self.conditions_class(commands=self.conditions)
        instance = self.state.instance
        return (permissions.execute(instance, user) and
                conditions.execute(instance))

    def get_available_actions(self, user=None, action_name=None):
        """
        It returns a list of available action names, every name is unique,
        in contrast with `get_available_transitions` where the transitions might have the same names.
        :param user: any object which used to validate permissions
        :param action_name: str
        :return: sorted list
        """
        return sorted(set([transition.action_name for transition in
                           self.get_available_transitions(user, action_name)]))

    def get_available_transitions(self, user=None, action_name=None, ignore_state=False, ignore_sources=False):
        """
        It returns all available transition which meet conditions and pass permissions.
        Including nested processes.
        :param user: any object which is used to validate permissions
        :param action_name: str
        :param ignore_state: skip the is_locked check (used internally by get_transition_by_action_name)
        :param ignore_sources: skip the source-state membership check, e.g. when
               the state has already been moved to in_progress_state by background mode phase 1
        :return: yield `django_logic.Transition`
        """
        if not self.is_valid(user):
            return

        if not ignore_state and self.state.is_locked():
            return

        for transition in self.transitions:
            if action_name is not None and transition.action_name != action_name:
                continue

            if (ignore_sources or self.state.get_state() in transition.sources) and transition.is_valid(self.state.instance, user):
                yield transition

        for sub_process_class in self.nested_processes:
            sub_process = sub_process_class(state=self.state)
            yield from sub_process.get_available_transitions(user=user, action_name=action_name,
                                                             ignore_state=ignore_state, ignore_sources=ignore_sources)

    def get_transition_by_action_name(self, action_name: str, user=None, ignore_sources=False):
        transitions = list(self.get_available_transitions(action_name=action_name, user=user,
                                                          ignore_state=True, ignore_sources=ignore_sources))
        if len(transitions) == 1:
            transition = transitions[0]
            # DEPRECATED
            self.logger.info(f"{self.state.instance_key}, process {self.process_name} "
                             f"executes '{action_name}' transition from {self.state.get_state()} "
                             f"to {transition.target}",
                             log_type=LogType.TRANSITION_DEBUG,
                             log_data=self.state.get_log_data())
            return transition
        elif len(transitions) > 1:
            # DEPRECATED
            self.logger.info(f"Runtime error: {self.state.instance_key} has several "
                             f"transitions with action name '{action_name}'. "
                             f"Make sure to specify conditions and permissions accordingly to fix such case",
                             log_type=LogType.TRANSITION_DEBUG,
                             log_data=self.state.get_log_data())
            logger.info(
                f"Runtime error: {self.state.instance_key} has several "
                f"transitions with action name '{action_name}'. "
                f"Make sure to specify conditions and permissions accordingly to fix such case"
                )
            raise TransitionNotAllowed("There are several transitions available")
        
        # DEPRECATED
        self.logger.info(f"Process class {self.__class__} for object {self.instance.id} has no transition "
                         f"with action name {action_name}, user {user}",
                         log_type=LogType.TRANSITION_DEBUG,
                         log_data=self.state.get_log_data())
        logger.info(
            f"Process class {self.__class__} for object {self.instance.id} has no transition "
            f"with action name {action_name}, user {user}"
            )
        raise TransitionNotAllowed(f"Process class {self.__class__} for object {self.instance.id} has no transition "
                                   f"with action name {action_name}, user {user}")


class ProcessManager:
    @classmethod
    def bind_model_process(cls, model, process_class, state_field: str = 'state') -> None:
        def make_process_getter(field_name, field_class):
            return lambda self: field_class(field_name=field_name, instance=self)

        setattr(model, process_class.process_name, property(make_process_getter(state_field, process_class)))

    @classmethod
    def bind_state_fields(cls, **kwargs):
        warnings.warn(
            "bind_state_fields is deprecated and will be removed in future versions. "
            "Use bind_model_process instead",
            DeprecationWarning
        )

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
        Returns a list of the object's non-state fields.
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
        State fields can be saved if explicitly passed in the 'update_fields' kwarg.
        """
        if self.id is not None and 'update_fields' not in kwargs:
            kwargs['update_fields'] = self.non_state_fields
        super().save(*args, **kwargs)
