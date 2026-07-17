"""Process — the binding layer between a model and its transitions.

A ``Process`` subclass declares a list of transitions and, optionally,
nested processes. ``ProcessManager.bind_model_process`` attaches the
process as a property on a Django model, after which callers use
``instance.my_process.action_name(...)`` to drive transitions.
"""
import inspect
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
        # Underscore/dunder names are never action names — refusing them
        # keeps introspection sane (copy/pickle/mock/IPython probe dunders
        # via getattr and must see a normal AttributeError, and
        # hasattr(process, '_x') must not be True for everything). Any
        # other missing attribute is assumed to be an action name and
        # resolved lazily at call time.
        if item.startswith('_'):
            raise AttributeError(item)

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
            # Defensive: drop a caller-supplied 'action_name' key, which
            # would otherwise collide with _get_transition_method's first
            # parameter ("multiple values for argument 'action_name'").
            # No engine path forwards it; only hand-built kwargs dicts do.
            kwargs.pop('action_name', None)
            return self._get_transition_method(item, **kwargs)

        return transition_method

    def _get_transition_method(self, action_name: str, **kwargs):
        parent_ctx = _transition_context.get()
        if parent_ctx:
            kwargs.setdefault('root_id', parent_ctx['root_id'])
            kwargs.setdefault('tr_id', parent_ctx['tr_id'])

        user = kwargs['user'] if 'user' in kwargs else None
        transition, owning_process = self._resolve_transition_with_owner(
            action_name, user
        )

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
        if getattr(transition, 'is_background', False):
            # Record the process class that DECLARES this transition. For a
            # nested transition this differs from ``process_class`` (the bound
            # process this call entered through); for a transition on the bound
            # process itself the two coincide. Phase-2 restore
            # (runner._find_transition) uses it to pick the exact background
            # transition when an ``action_name`` is shared across
            # condition-disambiguated nested processes. Overwrite, never
            # setdefault: a chained next_transition forwards the previous
            # transition's kwargs, and that owner is not this transition's.
            kwargs['owning_process_class'] = (
                f"{type(owning_process).__module__}."
                f"{type(owning_process).__name__}"
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
        for transition, _owner in self._iter_available_with_owner(
            user=user,
            action_name=action_name,
            ignore_state=ignore_state,
        ):
            yield transition

    def _iter_available_with_owner(
        self,
        user=None,
        action_name=None,
        ignore_state=False,
    ):
        """Like :meth:`get_available_transitions`, but yield
        ``(transition, owning_process)`` pairs.

        ``owning_process`` is the (possibly nested) ``Process`` instance that
        declared the transition — what phase 1 records so phase-2 restore can
        identify the exact background transition among condition-disambiguated
        siblings sharing an ``action_name``. Iteration order and filtering are
        identical to ``get_available_transitions``; that method is a thin
        wrapper that drops the owner.
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
                yield transition, self

        for sub_process_class in self.nested_processes:
            sub_process = sub_process_class(state=self.state)
            yield from sub_process._iter_available_with_owner(
                user=user,
                action_name=action_name,
                ignore_state=ignore_state,
            )

    def get_transition_by_action_name(self, action_name: str, user=None):
        transition, _owner = self._resolve_transition_with_owner(action_name, user)
        return transition

    def _resolve_transition_with_owner(self, action_name: str, user=None):
        """Resolve ``action_name`` to ``(transition, owning_process)``.

        Same disambiguation contract as ``get_transition_by_action_name``
        (exactly one match required, after conditions/permissions filtering
        with ``ignore_state=True``) — it just also returns the declaring
        process so the caller can record the owner for phase-2 restore.
        """
        matches = list(
            self._iter_available_with_owner(
                action_name=action_name,
                user=user,
                ignore_state=True,
            )
        )
        if len(matches) == 1:
            return matches[0]

        if len(matches) > 1:
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
    """A background transition must be uniquely identifiable by
    ``(owning process class, action_name)`` across a Process *and its nested
    processes*.

    Phase 1 records the owning (possibly nested) process class on the
    ``TransitionMessage`` (``owning_process_class``); phase-2 restore
    (``runner._find_transition``) uses it to select the exact background
    transition. So the only configuration phase 2 genuinely cannot resolve —
    and the only one rejected here — is **two background transitions sharing
    an ``action_name`` within a single process class**: the owner + name pair
    no longer identifies one transition.

    Everything else is allowed, because phase 2 can always resolve it:

    * The same background ``action_name`` on **distinct** nested process classes
      — the condition-disambiguated pattern (e.g. per-integration ``Gmail`` /
      ``Dummy`` sub-processes each declaring a background
      ``send_message_via_integration`` selected by a condition on the instance).
      Phase 1's ``get_transition_by_action_name`` resolves exactly one (the
      conditions are mutually exclusive); phase 2 restores that exact one via
      the recorded owner.
    * A background ``action_name`` that **coincides with a synchronous
      ``Transition``** of the same name. Phase 2 only ever restores background
      transitions and ``runner._find_transition`` filters to ``is_background``,
      so a synchronous namesake is invisible to restore. Phase 1 resolves the
      *call* by conditions/permissions exactly as it does for duplicate
      synchronous names — a genuinely ambiguous call raises
      ``TransitionNotAllowed`` at runtime, the same runtime-validated contract
      that already governs duplicate synchronous ``action_name``s (courier-style
      polymorphism).

    So the single structural invariant phase 2 needs — and all this validator
    enforces — is background-``action_name`` uniqueness *within one class*.
    """
    def _where(proc_cls, transition):
        return (
            f"{proc_cls.__module__}.{proc_cls.__name__}."
            f"{type(transition).__name__}"
        )

    for proc_cls in _iter_process_tree(process_cls):
        # Within ONE process class a background action_name must be unique —
        # (owning class, action_name) is phase 2's whole key, so two in the
        # same class are indistinguishable. Across classes, and against
        # synchronous transitions, duplicates are fine (resolved by conditions
        # at phase 1, by the owner + is_background filter at phase 2).
        local_background: dict[str, str] = {}
        for transition in proc_cls.transitions or []:
            if not getattr(transition, 'is_background', False):
                continue
            name = transition.action_name
            if name in local_background:
                raise ImproperlyConfigured(
                    f"Process {process_cls.__module__}."
                    f"{process_cls.__name__} (or its nested processes) "
                    f"has two background transitions sharing "
                    f"action_name='{name}' within a single process class "
                    f"({local_background[name]} and "
                    f"{_where(proc_cls, transition)}). Phase-2 restore "
                    f"identifies a background transition by (owning "
                    f"process class, action_name) — two in the same class "
                    f"are indistinguishable, so background action_names "
                    f"must be unique within a process class. Move one to "
                    f"a separate nested process (duplicates across "
                    f"distinct nested processes are allowed, disambiguated "
                    f"by conditions) or rename it."
                )
            local_background[name] = _where(proc_cls, transition)


def _validate_hook_signatures(process_cls) -> None:
    """Every hook must accept the instance as a named first positional
    parameter.

    A task-style ``def hook(*args, **kwargs)`` binds fine, receives the
    instance invisibly in ``args``, and typically reads ids out of kwargs
    the engine never passes — failing only at runtime, on the worker.
    Validating at bind time turns that latent failure into a boot-time
    signal. Covers transition-level hooks (side-effects, callbacks,
    failure hooks, conditions, permissions) and process-level
    ``conditions``/``permissions``. Warns by default;
    ``DJANGO_LOGIC['STRICT_HOOK_SIGNATURES'] = True`` raises
    ``ImproperlyConfigured`` instead.
    """
    from django.conf import settings

    offenders = []

    def check(fn, owner):
        try:
            params = list(inspect.signature(fn).parameters.values())
        except (TypeError, ValueError):
            return
        ok = params and params[0].kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        if not ok:
            offenders.append(
                f'{getattr(fn, "__module__", "?")}.'
                f'{getattr(fn, "__qualname__", fn)} (on {owner})'
            )

    _HOOK_ATTRS = (
        'side_effects', 'callbacks', 'failure_side_effects',
        'failure_callbacks', 'conditions', 'permissions',
    )
    for proc_cls in _iter_process_tree(process_cls):
        owner = f'{proc_cls.__module__}.{proc_cls.__name__}'
        # Process-level conditions/permissions are plain lists of callables
        # (executed via Conditions/Permissions in Process.is_valid). A
        # subclass may instead define them as a property/descriptor computed
        # per instance — those cannot be inspected at bind time; skip them.
        for attr in ('conditions', 'permissions'):
            hooks = getattr(proc_cls, attr, None)
            if isinstance(hooks, (list, tuple)):
                for fn in hooks:
                    check(fn, owner)
        for transition in proc_cls.transitions or []:
            # getattr-guarded: a duck-typed custom transition that the
            # engine never asks for one of these must not fail to bind.
            for attr in _HOOK_ATTRS:
                wrapper = getattr(transition, attr, None)
                for fn in getattr(wrapper, 'commands', None) or []:
                    check(fn, f'{owner}.{getattr(transition, "action_name", "?")}')
    if not offenders:
        return
    message = (
        'FSM hooks without a named instance-first parameter — the engine '
        'calls hooks as fn(instance, **kwargs) (permissions as '
        'fn(instance, user, **kwargs)), so give each hook a named first '
        'parameter, e.g. def hook(instance, **kwargs); decorated hooks '
        'need functools.wraps to expose the real signature: '
        f'{"; ".join(sorted(set(offenders)))}'
    )
    conf = getattr(settings, 'DJANGO_LOGIC', {}) or {}
    if conf.get('STRICT_HOOK_SIGNATURES', False):
        raise ImproperlyConfigured(message)
    transition_logger.warning(message)


class ProcessManager:
    @classmethod
    def bind_model_process(cls, model, process_class, state_field: str = 'state') -> None:
        _validate_hook_signatures(process_class)

        def make_process_getter(field_name, process_cls):
            return lambda self: process_cls(field_name=field_name, instance=self)

        setattr(
            model,
            process_class.process_name,
            property(make_process_getter(state_field, process_class)),
        )
