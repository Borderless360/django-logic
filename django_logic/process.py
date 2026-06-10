"""Process — the binding layer between a model and its transitions.

A ``Process`` subclass declares a list of transitions and, optionally,
nested processes. ``ProcessManager.bind_model_process`` attaches the
process as a property on a Django model, after which callers use
``instance.my_process.action_name(...)`` to drive transitions.
"""
import uuid
from contextvars import ContextVar

from django.core.exceptions import ImproperlyConfigured

from django_logic.commands import Conditions, Permissions
from django_logic.exceptions import TransitionNotAllowed
from django_logic.logger import transition_logger
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

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _validate_unique_in_progress_states(cls)
        _validate_unique_background_action_names(cls)

    def __init__(self, field_name='', instance=None, state=None):
        """Construct either from ``(instance, field_name)`` (normal path
        via ``instance.my_process``) or from an existing ``state`` object
        (nested-process path, to share the parent's state).
        """
        self.field_name = field_name
        self.instance = instance
        if state is not None:
            self.state = state
        else:
            assert field_name and instance is not None, (
                'Process requires either a state object or '
                '(field_name, instance).'
            )
            self.state = self.state_class(
                instance=instance,
                field_name=field_name,
                process_name=self.process_name,
            )

    def __getattr__(self, item):
        def transition_method(*args, **kwargs):
            if args:
                # Positional arguments used to be silently discarded — so
                # ``instance.process.verify(user)`` ran with user=None,
                # which BYPASSES all permission checks (and loses audit
                # attribution) without any error. Fail loudly instead.
                raise TypeError(
                    f"{item}() accepts keyword arguments only (got "
                    f"{len(args)} positional). Pass user and other values "
                    f"by keyword, e.g. {item}(user=request.user) — a "
                    f"positional user would be dropped and permission "
                    f"checks skipped."
                )
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
    ):
        """Yield transitions whose conditions/permissions pass.

        :param ignore_state: skip the ``is_locked`` check (internal use by
            ``get_transition_by_action_name``).
        """
        if not self.is_valid(user):
            return

        if not ignore_state and self.state.is_locked():
            return

        for transition in self.transitions:
            if action_name is not None and transition.action_name != action_name:
                continue

            if (
                self.state.get_state() in transition.sources
                and transition.is_valid(self.state.instance, user)
            ):
                yield transition

        for sub_process_class in self.nested_processes:
            sub_process = sub_process_class(state=self.state)
            yield from sub_process.get_available_transitions(
                user=user,
                action_name=action_name,
                ignore_state=ignore_state,
            )

    def get_transition_by_action_name(self, action_name: str, user=None):
        transitions = list(
            self.get_available_transitions(
                action_name=action_name,
                user=user,
                ignore_state=True,
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
            f"{self.state.instance.pk} has no transition "
            f"with action name {action_name}, user {user}"
        )
        raise TransitionNotAllowed(
            f"Process class {self.__class__} for object "
            f"{self.state.instance.pk} has no transition "
            f"with action name {action_name}, user {user}"
        )


def _validate_unique_in_progress_states(process_cls):
    """Reject duplicate in_progress_state values across a Process AND its
    nested processes.

    Unique ``in_progress_state`` is what lets the phase-2 background
    transition lookup work unambiguously — the in-progress state alone
    identifies the transition that's mid-flight, without any source-state
    search. Nested processes share the parent's state field, so the
    uniqueness guarantee must hold across the whole tree (mirroring
    ``_validate_unique_background_action_names``), not just the class's
    own ``transitions``.
    """
    seen: dict[str, str] = {}
    for proc_cls in _iter_process_tree(process_cls):
        for transition in proc_cls.transitions or []:
            in_progress = getattr(transition, 'in_progress_state', None)
            if not in_progress:
                continue
            where = (
                f'{proc_cls.__module__}.{proc_cls.__name__}.'
                f'{transition.action_name}'
            )
            if in_progress in seen:
                raise ImproperlyConfigured(
                    f"Process {process_cls.__module__}.{process_cls.__name__} "
                    f"(or its nested processes) has two transitions sharing "
                    f"in_progress_state='{in_progress}': {seen[in_progress]} "
                    f"and {where}. Every in_progress_state must be unique "
                    f"across a Process and its nested processes."
                )
            seen[in_progress] = where


def _iter_process_tree(process_cls, _seen=None):
    """Yield ``process_cls`` and every Process class reachable through
    ``nested_processes`` (depth-first), guarding against cycles.

    Reads only class-level attributes, so it is safe to call at
    class-creation time: every class listed in ``nested_processes`` is
    already defined by the time the parent class body runs.
    """
    if _seen is None:
        _seen = set()
    if id(process_cls) in _seen:
        return
    _seen.add(id(process_cls))
    yield process_cls
    for sub_process_cls in process_cls.nested_processes or []:
        yield from _iter_process_tree(sub_process_cls, _seen)


def _validate_unique_background_action_names(process_cls):
    """Background transitions must be uniquely identifiable by ``action_name``
    across a Process *and its nested processes*.

    Phase-2 restore (``runner._find_transition``) looks a transition up by
    ``TransitionMessage.transition_name`` (= the ``action_name``) alone,
    searching the bound process and descending into its ``nested_processes``
    — it has no other discriminator. So, across the whole nested tree:

    - No two ``BackgroundTransition`` / ``BackgroundAction`` instances may
      share an ``action_name``.
    - A background ``action_name`` may not also appear on a plain
      synchronous ``Transition``; phase 2 would pick whichever it reached
      first and could grab the sync one.

    Sync-only ``action_name`` duplication is still allowed (the sync call
    path uses ``get_transition_by_action_name`` which disambiguates via
    conditions/permissions at runtime) — this is what lets nested processes
    model courier-style polymorphism (many sub-processes, same action name,
    different conditions).
    """
    def _where(proc_cls, transition):
        return (
            f"{proc_cls.__module__}.{proc_cls.__name__}."
            f"{type(transition).__name__}"
        )

    background_names: dict[str, str] = {}
    sync_names: dict[str, str] = {}

    for proc_cls in _iter_process_tree(process_cls):
        for transition in proc_cls.transitions or []:
            name = transition.action_name
            if getattr(transition, 'is_background', False):
                if name in background_names:
                    raise ImproperlyConfigured(
                        f"Process {process_cls.__module__}."
                        f"{process_cls.__name__} (or its nested processes) "
                        f"has two background transitions sharing "
                        f"action_name='{name}' ({background_names[name]} "
                        f"and {_where(proc_cls, transition)}). Phase-2 "
                        f"restore searches the process and its "
                        f"nested_processes and uses action_name as its only "
                        f"key — background action_names must be unique "
                        f"across a Process and its nested processes."
                    )
                background_names[name] = _where(proc_cls, transition)
            else:
                sync_names.setdefault(name, _where(proc_cls, transition))

    for name, bg_where in background_names.items():
        if name in sync_names:
            raise ImproperlyConfigured(
                f"Process {process_cls.__module__}.{process_cls.__name__} "
                f"(or its nested processes) has a synchronous Transition "
                f"named '{name}' ({sync_names[name]}) that collides with a "
                f"background transition of the same name ({bg_where}). "
                f"Phase-2 restore searches the process tree and picks the "
                f"first matching action_name — it cannot distinguish them; "
                f"rename one."
            )


class ProcessManager:
    @classmethod
    def bind_model_process(cls, model, process_class, state_field: str = 'state') -> None:
        def make_process_getter(field_name, process_cls):
            return lambda self: process_cls(field_name=field_name, instance=self)

        setattr(
            model,
            process_class.process_name,
            property(make_process_getter(state_field, process_class)),
        )
