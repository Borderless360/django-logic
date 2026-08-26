"""Process — the binding layer between a model and its transitions.

A ``Process`` subclass declares a list of transitions and, optionally,
nested processes. ``ProcessManager.bind_model_process`` attaches the
process as a property on a Django model, after which callers use
``instance.my_process.action_name(...)`` to drive transitions.
"""
import inspect
import uuid
from collections import namedtuple
from contextvars import ContextVar

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured

from django_logic.commands import Conditions, Permissions
from django_logic.exceptions import TransitionNotAllowed
from django_logic.logger import transition_logger
from django_logic.state import State


# Per-execution-chain context that propagates transition metadata
# (root_id, tr_id) through nested callbacks without explicit kwargs forwarding.
_transition_context: ContextVar[dict | None] = ContextVar(
    '_transition_context', default=None
)

class _ProcessAccessor(property):
    """The model property ``bind_model_process`` installs.

    A distinct subclass purely so a later binding can tell "django-logic put
    this here" from "the model owns this attribute" — ``property`` objects
    cannot carry a marker attribute. See the collision check in
    ``bind_model_process``.
    """


#: Attributes ``Process.__init__`` sets on the instance. They shadow
#: ``__getattr__`` exactly as class attributes do, but ``hasattr(cls, name)``
#: cannot see them.
_PROCESS_INSTANCE_ATTRS = frozenset({'state', 'instance', 'field_name'})

class Process:
    """Declarative container of transitions and nested processes.

    Subclasses declare class-level attributes ``transitions``,
    ``nested_processes``, ``conditions``, ``permissions``,
    ``process_name``, and ``state_class``.

    Class-time validation enforces that a background transition is
    uniquely identifiable by ``(process class that declared it,
    action_name)``, which is what the worker uses to restore a
    ``TransitionMessage``.
    """

    nested_processes = []
    transitions = []
    conditions = []
    permissions = []
    permissions_class = Permissions
    state_class = State
    process_name = 'process'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _validate_action_names_not_shadowed(cls)
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
            if not field_name or instance is None:
                # A real exception, not an assert — asserts vanish under
                # python -O and this is the constructor's only guard.
                raise TypeError(
                    'Process requires either a state object or '
                    '(field_name, instance).'
                )
            self.state = self.state_class(
                instance=instance,
                field_name=field_name,
                process_name=self.process_name,
            )

    def __getattr__(self, item):
        # Names that start with an underscore are never action names. copy,
        # pickle, mock and IPython probe dunder names with getattr and must get
        # a normal AttributeError, and hasattr(process, '_x') must not be True
        # for every name. Any other missing attribute is treated as an action
        # name and resolved when it is called.
        if item.startswith('_'):
            raise AttributeError(item)

        def transition_method(*args, **kwargs):
            if args:
                # Positional arguments used to be dropped, so
                # ``instance.process.verify(user)`` ran with user=None. That
                # skips every permission check and loses the audit trail with
                # no error at all, so fail loudly instead.
                raise TypeError(
                    f"{item}() accepts keyword arguments only (got "
                    f"{len(args)} positional). Pass user and other values "
                    f"by keyword, e.g. {item}(user=request.user) — a "
                    f"positional user would be dropped and permission "
                    f"checks skipped."
                )
            # Drop an 'action_name' key the caller passed: it would clash with
            # _get_transition_method's first parameter and raise "multiple
            # values for argument 'action_name'". No engine path forwards it;
            # only a hand-built kwargs dict does. The reserved engine names
            # stay, because next_transition forwards tr_id, root_id,
            # parent_id and process_class through this same path.
            kwargs.pop('action_name', None)
            return self._get_transition_method(item, **kwargs)

        # Django's template engine CALLS any callable it resolves, so
        # ``{{ order.process.approve }}`` used to drive the state machine
        # while rendering a page and print the tr_id. ``alters_data``
        # is the framework's opt-out — the same marker Model.save/delete
        # carry — and makes the engine render '' instead of calling.
        transition_method.alters_data = True

        return transition_method

    def _get_transition_method(self, action_name: str, **kwargs):
        parent_ctx = _transition_context.get()
        if parent_ctx:
            kwargs.setdefault('root_id', parent_ctx['root_id'])
            kwargs.setdefault('tr_id', parent_ctx['tr_id'])

        user = kwargs.get('user')
        transition, owning_process = self._resolve_transition_with_owner(
            action_name, user
        )

        tr_id = uuid.uuid4()
        target_note = (
            f"to {transition.target}" if transition.target is not None
            else "(no state write)"
        )
        transition_logger.info(
            f"{tr_id} {self.state.instance_key}, process {self.process_name} "
            f"executes '{action_name}' transition from {self.state.get_state()} "
            f"{target_note}  "
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
            # process itself the two coincide. Worker restore
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
        conditions = Conditions(commands=self.conditions)
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

    def get_available_transitions(self, user=None, action_name=None):
        """Yield transitions whose conditions/permissions pass."""
        for transition, _owner in self._iter_available_with_owner(
            user=user,
            action_name=action_name,
        ):
            yield transition

    def _iter_available_with_owner(
        self,
        user=None,
        action_name=None,
        ignore_state=False,
        _seen=None,
    ):
        """Like :meth:`get_available_transitions`, but yield
        ``(transition, owning_process)`` pairs.

        ``owning_process`` is the (possibly nested) ``Process`` instance that
        declared the transition — what enqueue records so the worker can
        identify the exact background transition among siblings that share
        an ``action_name`` and use conditions to choose. Iteration order
        and filtering are identical to ``get_available_transitions``; that
        method is a thin wrapper that drops the owner.
        """
        # Visit each Process CLASS once per walk: a class reachable by two
        # paths shares its transition objects, so it must yield them once
        # (twice reads as "several transitions available" and no condition
        # can satisfy the hint), and a nested cycle must terminate. Deduping
        # by class keeps one ``action_name`` on *distinct* nested classes
        # legal. Mirrors ``_iter_process_tree``.
        if _seen is None:
            _seen = set()
        if id(type(self)) in _seen:
            return
        _seen.add(id(type(self)))

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
                _seen=_seen,
            )

    def _resolve_transition_with_owner(self, action_name: str, user=None):
        """Resolve ``action_name`` to ``(transition, owning_process)``.

        Exactly one match is required, after conditions/permissions
        filtering with ``ignore_state=True``. Also returns the declaring
        process so the caller can record it for worker restore.
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

        current_state = self.state.get_state()
        try:
            available_actions = self.get_available_actions(user=user)
        except Exception as list_error:
            # Listing the actions runs every transition's conditions and
            # permissions. One of them raising must not replace the
            # refusal with its own exception — the caller asked why the
            # action was refused, and it still gets that answer.
            transition_logger.info(
                f'Listing the available actions for '
                f'{self.state.instance_key} failed: '
                f'{type(list_error).__name__}: {list_error}'
            )
            available_actions = None
        known_actions = (
            'unknown' if available_actions is None
            else ', '.join(available_actions) or 'none'
        )
        message = (
            f"Process class {self.__class__} for object "
            f"{self.state.instance.pk} has no transition "
            f"with action name {action_name}, user {user}. "
            f"The instance is in state {current_state!r}; "
            f"available actions: {known_actions}."
        )
        transition_logger.info(message)
        error = TransitionNotAllowed(message)
        error.current_state = current_state
        error.available_actions = available_actions
        raise error


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


def _validate_action_names_not_shadowed(process_cls):
    """Reject an ``action_name`` that a real attribute of the ROOT process
    shadows (raised at class creation).

    ``__getattr__`` only runs when normal attribute lookup *fails*, so a
    transition named ``state``, ``is_valid``, ``transitions``,
    ``process_name``… is unreachable: ``instance.process.is_valid()`` calls
    the bound method and returns ``True`` without transitioning anything.
    ``get_available_actions()`` still advertises the name, so the transition
    looks live and silently does nothing — the worst possible failure mode
    for a state machine. There is no way to reach it, so this is a
    definition error, not a runtime concern.

    Only the root's own attributes can shadow, because dispatch enters
    through the BOUND process's ``__getattr__`` — a nested class is never
    the lookup target. The root alone is enough: every ``Process``
    subclass is validated as its own root when it is defined.
    """
    for proc_cls in _iter_process_tree(process_cls):
        for transition in proc_cls.transitions or []:
            action_name = getattr(transition, 'action_name', None)
            if not action_name:
                continue
            # Transitions of the whole tree, attributes of the root only:
            # a nested transition is called through the bound (root) class,
            # so that is the class whose attributes shadow it.
            if action_name in _PROCESS_INSTANCE_ATTRS:
                shadowed_by = (
                    f'an attribute Process.__init__ sets ({action_name})')
            elif hasattr(process_cls, action_name):
                shadowed_by = (
                    f'{process_cls.__name__}.{action_name} '
                    f'({getattr(process_cls, action_name)!r})')
            else:
                continue
            raise ImproperlyConfigured(
                f"Process {proc_cls.__module__}.{proc_cls.__name__} declares "
                f"a transition named {action_name!r}, which is shadowed by "
                f"{shadowed_by}. Attribute lookup wins over the lazy action "
                f"dispatcher, so the transition could never be called — "
                f"rename the action."
            )


def _validate_unique_background_action_names(process_cls):
    """A background ``action_name`` must be unique within one process class.

    ``(process class that declared it, action_name)`` is the worker's
    whole restore key: enqueue records the declaring class on the row,
    and restore selects by that class plus the name. Two background
    transitions sharing a name in one class are indistinguishable, so
    they are rejected here. Everything else stays legal: the same name
    on distinct nested classes is resolved by conditions at enqueue and
    by the recorded class at restore, and a synchronous namesake is
    invisible to restore (it filters to ``is_background``).
    """
    def _where(proc_cls, transition):
        return (
            f"{proc_cls.__module__}.{proc_cls.__name__}."
            f"{type(transition).__name__}"
        )

    for proc_cls in _iter_process_tree(process_cls):
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
                    f"{_where(proc_cls, transition)}). The worker restores "
                    f"a background transition by (declaring class, "
                    f"action_name), so background action_names must be "
                    f"unique within a process class. Move one to a "
                    f"separate nested process or rename it."
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
    ``conditions``/``permissions``. Raises ``ImproperlyConfigured`` at
    bind time.
    """
    offenders = collect_hook_signature_offenders(process_cls)
    if offenders:
        raise ImproperlyConfigured(
            'FSM hooks without a named instance-first parameter — the engine '
            'calls hooks as fn(instance, **kwargs) (permissions as '
            'fn(instance, user, **kwargs)), so give each hook a named first '
            'parameter, e.g. def hook(instance, **kwargs); decorated hooks '
            'need functools.wraps to expose the real signature: '
            f'{"; ".join(sorted(set(offenders)))}'
        )
    request_readers = collect_request_param_offenders(process_cls)
    if request_readers:
        raise ImproperlyConfigured(
            'FSM hooks that name a request parameter — a transition never '
            'takes the request (it is refused at the call, and a worker has '
            'none), so a hook can never receive one. Resolve what the hook '
            'needs at the call site and pass plain values: '
            f'{"; ".join(sorted(set(request_readers)))}'
        )


def collect_hook_signature_offenders(process_cls) -> list:
    """Every hook across ``process_cls``'s tree whose first parameter is not
    a named positional, as ``module.qualname (on Owner[.action])`` strings.
    Pure collection — bind-time validation enforces.
    """
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
        'side_effects', 'callbacks', 'failure_callbacks',
        'conditions', 'permissions',
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
    return offenders


def collect_request_param_offenders(process_cls) -> list:
    """Every hook across ``process_cls``'s tree that names a ``request``
    parameter, as ``module.qualname (on Owner[.action])`` strings. A
    transition never takes the request, so such a hook waits for a value
    that can never arrive. Pure collection — bind-time validation enforces.
    """
    offenders = []

    def check(fn, owner):
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return
        if 'request' in params:
            offenders.append(
                f'{getattr(fn, "__module__", "?")}.'
                f'{getattr(fn, "__qualname__", fn)} (on {owner})'
            )

    _HOOK_ATTRS = (
        'side_effects', 'callbacks', 'failure_callbacks',
        'conditions', 'permissions',
    )
    for proc_cls in _iter_process_tree(process_cls):
        owner = f'{proc_cls.__module__}.{proc_cls.__name__}'
        for attr in ('conditions', 'permissions'):
            hooks = getattr(proc_cls, attr, None)
            if isinstance(hooks, (list, tuple)):
                for fn in hooks:
                    check(fn, owner)
        for transition in proc_cls.transitions or []:
            for attr in _HOOK_ATTRS:
                wrapper = getattr(transition, attr, None)
                for fn in getattr(wrapper, 'commands', None) or []:
                    check(fn, f'{owner}.{getattr(transition, "action_name", "?")}')
    return offenders


#: One record per ``bind_model_process`` call.
ModelProcessBinding = namedtuple(
    'ModelProcessBinding', ['model', 'process_class', 'state_field'])


class ProcessManager:
    #: Public registry of every bound machine, in bind order. Consumer
    #: tooling (coverage audits, contract tests, the ``django_logic``
    #: system check) reads this instead of re-deriving bindings from
    #: model attributes.
    bindings: list = []

    @classmethod
    def bind_model_process(cls, model, process_class, state_field: str = 'state') -> None:
        binding = ModelProcessBinding(model, process_class, state_field)
        if binding in cls.bindings:
            # Identical re-bind (an AppConfig.ready() running twice, a
            # test re-import) is a harmless no-op — the model property
            # and registry entry are already in place.
            return

        # A background transition writes a TransitionMessage row, and the
        # 'django_logic' app owns that table, its migrations and every
        # system check. Without the app nothing would report the gap, so
        # the first background transition would die on a missing table.
        from django.apps import apps as django_apps

        from django_logic.checks import _process_tree_has_background_transition

        if (_process_tree_has_background_transition(process_class)
                and not django_apps.is_installed('django_logic')):
            raise ImproperlyConfigured(
                f"{model._meta.label} binds {process_class.__name__}, which "
                f"declares a background transition, but 'django_logic' is "
                f"not in INSTALLED_APPS. Add it and run manage.py migrate."
            )

        # The state field must be a concrete column: a typo, a property,
        # or a relation silently accepted here only fails much later —
        # deep inside a transition's state write or the stranded sweep.
        try:
            field = model._meta.get_field(state_field)
        except FieldDoesNotExist as exc:
            raise ImproperlyConfigured(
                f"bind_model_process({model._meta.label}, "
                f"{process_class.__name__}): state_field {state_field!r} "
                f"is not a field on {model._meta.label}."
            ) from exc
        if not field.concrete:
            raise ImproperlyConfigured(
                f"bind_model_process({model._meta.label}, "
                f"{process_class.__name__}): state_field {state_field!r} "
                f"must be a concrete model field (got {type(field).__name__})."
            )

        # A different binding under the same process_name would silently
        # overwrite the model property while its registry entry kept
        # claiming the old machine — every registry consumer (coverage,
        # system checks, stranded recovery) would then disagree with
        # runtime dispatch.
        for existing in cls.bindings:
            if (existing.model is model
                    and existing.process_class.process_name
                    == process_class.process_name):
                raise ImproperlyConfigured(
                    f"bind_model_process({model._meta.label}, "
                    f"{process_class.__name__}): process_name "
                    f"{process_class.process_name!r} is already bound on "
                    f"this model (to {existing.process_class.__name__} on "
                    f"state_field {existing.state_field!r}). Give one of "
                    f"the processes a distinct process_name."
                )

        # setattr below replaces whatever descriptor the name currently
        # holds, so check what would actually be overwritten: any attribute
        # defined anywhere on the model's MRO (not _meta field names — a
        # model method, property, or manager can clash too, and `process`
        # is a very plausible method name). Skip accessors django-logic
        # itself installed: a multi-table-inheritance child legitimately
        # re-binds its parent's process_name, and its own accessor shadows
        # the parent's.
        clashing = next(
            (klass for klass in model.__mro__
             if process_class.process_name in vars(klass)
             and not isinstance(
                 vars(klass)[process_class.process_name], _ProcessAccessor)),
            None,
        )
        if clashing is not None:
            existing = vars(clashing)[process_class.process_name]
            raise ImproperlyConfigured(
                f"bind_model_process({model._meta.label}, "
                f"{process_class.__name__}): process_name "
                f"{process_class.process_name!r} already names something on "
                f"{clashing.__name__} ({existing!r}). Binding would replace "
                f"it and break the model. Give the process a distinct "
                f"process_name."
            )

        _validate_hook_signatures(process_class)
        cls.bindings.append(binding)

        def make_process_getter(field_name, process_cls):
            return lambda self: process_cls(field_name=field_name, instance=self)

        setattr(
            model,
            process_class.process_name,
            _ProcessAccessor(make_process_getter(state_field, process_class)),
        )

    @classmethod
    def unbind_model_process(
        cls, model, process_class=None, state_field: str | None = None,
    ) -> None:
        """Inverse of :meth:`bind_model_process`: drop the registry entries
        and the model accessor they installed.

        Exists for teardown — a consumer (and this library's own suite)
        binding a throwaway process in a test otherwise has to rewrite
        ``ProcessManager.bindings`` and ``delattr`` the accessor by hand,
        and every copy of that gets the multi-binding cases wrong.
        ``process_class`` / ``state_field`` narrow which of the model's
        bindings to remove; omitting both removes all of them. Unbinding
        something that was never bound is a no-op.
        """
        removed = [
            binding for binding in cls.bindings
            if binding.model is model
            and (process_class is None or binding.process_class is process_class)
            and (state_field is None or binding.state_field == state_field)
        ]
        if not removed:
            return
        cls.bindings = [b for b in cls.bindings if b not in removed]

        # One model can carry several accessors (one per process_name, e.g.
        # a second state_field's machine), so only the names no surviving
        # binding of this model still claims may go — and only when
        # django-logic owns them: an MTI child inherits its parent's
        # accessor, which is the parent binding's to remove, not ours.
        still_bound = {
            binding.process_class.process_name for binding in cls.bindings
            if binding.model is model
        }
        for binding in removed:
            name = binding.process_class.process_name
            if name in still_bound:
                continue
            if isinstance(vars(model).get(name), _ProcessAccessor):
                delattr(model, name)
