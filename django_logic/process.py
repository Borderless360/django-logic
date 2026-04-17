"""Process — the binding layer between a model and its transitions.

A ``Process`` subclass declares a list of transitions and, optionally,
nested processes. ``ProcessManager.bind_model_process`` attaches the
process as a property on a Django model, after which callers use
``instance.my_process.action_name(...)`` to drive transitions.
"""
import uuid
import warnings
from contextvars import ContextVar

from django.core.exceptions import ImproperlyConfigured

from django_logic.commands import Conditions, Permissions
from django_logic.exceptions import TransitionNotAllowed
from django_logic.logger import transition_logger, TransitionEventType
from django_logic.state import State


# Per-execution-chain context that propagates transition metadata
# (root_id, tr_id) through nested callbacks without explicit kwargs forwarding.
_transition_context: ContextVar[dict | None] = ContextVar(
    '_transition_context', default=None
)


class Process:
    """Declarative container of transitions and nested processes.

    Subclasses declare class-level attributes ``transitions``,
    ``nested_processes``, ``conditions``, ``permissions``,
    ``process_name``, and ``state_class``.

    Class-time validation enforces that no two transitions on the same
    ``Process`` share the same ``in_progress_state`` — this makes
    phase-2 background-transition lookup unambiguous.
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

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _validate_unique_in_progress_states(cls)

    def __init__(self, field_name='', instance=None, state=None):
        self.field_name = field_name
        self.instance = instance
        if field_name == '' or instance is None:
            assert state is not None
            self.state = state
        elif state is None:
            assert field_name and instance is not None
            self.state = self.state_class(
                queryset_name=self.queryset_name,
                instance=instance,
                field_name=field_name,
                process_name=self.process_name,
            )
        else:
            raise AttributeError(
                'Process class requires either state field name and instance '
                'or state object'
            )

    def __getattr__(self, item):
        def transition_method(*args, **kwargs):
            # Strip action_name from kwargs in case it was forwarded from a
            # parent invocation (Celery restore, nested call); otherwise we'd
            # get "multiple values for argument 'action_name'" below.
            kwargs.pop('action_name', None)
            return self._get_transition_method(item, **kwargs)

        return transition_method

    def _get_transition_method(self, action_name: str, **kwargs):
        parent_ctx = _transition_context.get()
        if parent_ctx:
            kwargs.setdefault('root_id', parent_ctx['root_id'])
            kwargs.setdefault('tr_id', parent_ctx['tr_id'])

        user = kwargs['user'] if 'user' in kwargs else None
        transition = self.get_transition_by_action_name(action_name, user)

        tr_id = uuid.uuid4()
        transition_logger.info(
            f"{tr_id} {self.state.instance_key}, process {self.process_name} "
            f"executes '{action_name}' transition from {self.state.get_state()} "
            f"to {transition.target}  "
        )
        kwargs['root_id'] = kwargs.get('root_id', tr_id)
        kwargs['parent_id'] = kwargs.get('tr_id', tr_id)
        kwargs['tr_id'] = tr_id
        if 'process_class' not in kwargs:
            kwargs['process_class'] = (
                f"{self.__class__.__module__}.{self.__class__.__name__}"
            )

        token = _transition_context.set(
            {'root_id': kwargs['root_id'], 'tr_id': kwargs['tr_id']}
        )
        try:
            return transition.change_state(self.state, **kwargs)
        finally:
            _transition_context.reset(token)

    def is_valid(self, user=None) -> bool:
        permissions = self.permissions_class(commands=self.permissions)
        conditions = self.conditions_class(commands=self.conditions)
        instance = self.state.instance
        return permissions.execute(instance, user) and conditions.execute(instance)

    def get_available_actions(self, user=None, action_name=None):
        """Return a sorted list of unique action names currently available."""
        return sorted(
            {
                transition.action_name
                for transition in self.get_available_transitions(user, action_name)
            }
        )

    def get_available_transitions(
        self,
        user=None,
        action_name=None,
        ignore_state=False,
        ignore_sources=False,
    ):
        """Yield transitions whose conditions/permissions pass.

        :param ignore_state: skip the ``is_locked`` check (internal use by
            ``get_transition_by_action_name``).
        :param ignore_sources: skip the source-state membership check.
            Not normally needed since ``in_progress_state`` uniqueness
            lets phase-2 lookup work via the target-side state.
        """
        if not self.is_valid(user):
            return

        if not ignore_state and self.state.is_locked():
            return

        for transition in self.transitions:
            if action_name is not None and transition.action_name != action_name:
                continue

            if (
                ignore_sources
                or self.state.get_state() in transition.sources
            ) and transition.is_valid(self.state.instance, user):
                yield transition

        for sub_process_class in self.nested_processes:
            sub_process = sub_process_class(state=self.state)
            yield from sub_process.get_available_transitions(
                user=user,
                action_name=action_name,
                ignore_state=ignore_state,
                ignore_sources=ignore_sources,
            )

    def get_transition_by_action_name(
        self, action_name: str, user=None, ignore_sources=False
    ):
        transitions = list(
            self.get_available_transitions(
                action_name=action_name,
                user=user,
                ignore_state=True,
                ignore_sources=ignore_sources,
            )
        )
        if len(transitions) == 1:
            return transitions[0]

        if len(transitions) > 1:
            transition_logger.info(
                f"Runtime error: {self.state.instance_key} has several "
                f"transitions with action name '{action_name}'. "
                f"Specify conditions and permissions to disambiguate."
            )
            raise TransitionNotAllowed("There are several transitions available")

        transition_logger.info(
            f"Process class {self.__class__} for object "
            f"{self.state.instance.id} has no transition "
            f"with action name {action_name}, user {user}"
        )
        raise TransitionNotAllowed(
            f"Process class {self.__class__} for object "
            f"{self.state.instance.id} has no transition "
            f"with action name {action_name}, user {user}"
        )


def _validate_unique_in_progress_states(process_cls):
    """Reject duplicate in_progress_state values within a single Process.

    Unique ``in_progress_state`` is what lets phase-2 background transition
    lookup work without the ``ignore_sources`` escape hatch — the in-progress
    state alone identifies the transition that's mid-flight.
    """
    seen: dict[str, str] = {}
    for transition in process_cls.transitions or []:
        in_progress = getattr(transition, 'in_progress_state', None)
        if not in_progress:
            continue
        if in_progress in seen:
            raise ImproperlyConfigured(
                f"Process {process_cls.__module__}.{process_cls.__name__} "
                f"has two transitions sharing in_progress_state="
                f"'{in_progress}': '{seen[in_progress]}' and "
                f"'{transition.action_name}'. Every in_progress_state must "
                f"be unique within a Process."
            )
        seen[in_progress] = transition.action_name


class ProcessManager:
    @classmethod
    def bind_model_process(cls, model, process_class, state_field: str = 'state') -> None:
        def make_process_getter(field_name, field_class):
            return lambda self: field_class(field_name=field_name, instance=self)

        setattr(
            model,
            process_class.process_name,
            property(make_process_getter(state_field, process_class)),
        )

    @classmethod
    def bind_state_fields(cls, **kwargs):
        warnings.warn(
            "bind_state_fields is deprecated and will be removed in future versions. "
            "Use bind_model_process instead",
            DeprecationWarning,
            stacklevel=2,
        )

        def make_process_getter(field_name, field_class):
            return lambda self: field_class(field_name=field_name, instance=self)

        parameters: dict = {'state_fields': []}
        for state_field, process_class in kwargs.items():
            if not issubclass(process_class, Process):
                raise TypeError('Must be a sub class of Process')
            parameters[process_class.process_name] = property(
                make_process_getter(state_field, process_class)
            )
            parameters['state_fields'].append(state_field)
        return type('Process', (cls,), parameters)

    @property
    def non_state_fields(self):
        field_names = set()
        for field in self._meta.fields:
            if not field.primary_key and field.name not in self.state_fields:
                field_names.add(field.name)
                if field.name != field.attname:
                    field_names.add(field.attname)
        return field_names

    def save(self, *args, **kwargs):
        """Save non-state fields by default.

        State fields are saved only when explicitly passed via ``update_fields``.
        """
        if self.id is not None and 'update_fields' not in kwargs:
            kwargs['update_fields'] = self.non_state_fields
        super().save(*args, **kwargs)
