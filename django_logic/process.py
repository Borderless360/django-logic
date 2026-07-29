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

#: Transition-initiation observers. Each callable is invoked as
#: ``observer(owning_process_cls, action_name, instance, transition)``
#: after a transition resolves, before it executes — for every initiation
#: path (direct calls, next_transition follow-ups, background phase 1;
#: phase-2 restore does not re-notify). ``transition`` is the resolved
#: declaration object, so condition-disambiguated same-name transitions
#: are distinguishable (#146; the argument was added in 0.9 — observers
#: written against the 0.8 three-argument form need a ``transition=None``
#: parameter added). Observers must never break a transition: exceptions
#: are logged and swallowed. Registered by ``django_logic.coverage``;
#: open to consumer metrics/tracing hooks.
transition_observers: list = []


def _notify_transition_observers(owning_process, action_name, instance, transition):
    for observer in tuple(transition_observers):
        try:
            observer(type(owning_process), action_name, instance, transition)
        except Exception:
            transition_logger.exception(
                f'transition observer {observer!r} raised; ignored'
            )


#: Kwarg names the engine sets on every drive, and forwards through
#: ``__getattr__`` itself when chaining a ``next_transition`` follow-up. A
#: caller that passes one gets it silently overwritten, so they are documented
#: as reserved rather than refused — the engine cannot distinguish its own
#: forwarding from a caller's at that layer.
#: Attributes ``Process.__init__`` sets on the instance. They shadow
#: ``__getattr__`` exactly as class attributes do, but ``hasattr(cls, name)``
#: cannot see them.
_PROCESS_INSTANCE_ATTRS = frozenset({'state', 'instance', 'field_name'})

_RESERVED_KWARGS = frozenset({
    'tr_id', 'root_id', 'parent_id', 'process_class', 'owning_process_class',
})


class Process:
    """Declarative container of transitions and nested processes.

    Subclasses declare class-level attributes ``transitions``,
    ``nested_processes``, ``conditions``, ``permissions``,
    ``process_name``, and ``state_class``.

    Class-time validation enforces that a background transition is
    uniquely identifiable by ``(owning process class, action_name)``,
    which is what phase-2 restore resolves a ``TransitionMessage`` by.
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
            # The other reserved names (_RESERVED_KWARGS) are NOT refused
            # here: next_transition chaining forwards tr_id / root_id /
            # parent_id / process_class through this very path to propagate
            # lineage, so the engine cannot tell its own forwarding from a
            # caller's. They are documented as reserved in the README instead.
            kwargs.pop('action_name', None)
            return self._get_transition_method(item, **kwargs)

        # Django's template engine CALLS any callable it resolves, so
        # ``{{ order.process.approve }}`` used to drive the state machine
        # while rendering a page and print the tr_id (#181). ``alters_data``
        # is the framework's opt-out — the same marker Model.save/delete
        # carry — and makes the engine render '' instead of calling.
        transition_method.alters_data = True

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
        if transition_observers:
            _notify_transition_observers(
                owning_process, action_name, self.state.instance, transition
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
        declared the transition — what phase 1 records so phase-2 restore can
        identify the exact background transition among condition-disambiguated
        siblings sharing an ``action_name``. Iteration order and filtering are
        identical to ``get_available_transitions``; that method is a thin
        wrapper that drops the owner.
        """
        # Visit each Process CLASS once per walk (#180). Without this, a
        # nested process reachable by two paths — a diamond, or a duplicated
        # entry in ``nested_processes`` — yielded every one of its
        # transitions twice, and ``_resolve_transition_with_owner`` rejected
        # the single declaration as "several transitions available", with a
        # hint no condition could satisfy (both matches being the same
        # object). ``get_available_actions`` set-dedupes, so the action was
        # advertised and then failed on every call. A nested cycle recursed
        # until RecursionError. Deduping by class preserves the supported
        # pattern of one ``action_name`` on *distinct* nested classes
        # disambiguated by conditions. Mirrors ``_iter_process_tree``.
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
    """Reject an ``action_name`` that a real ``Process`` attribute shadows
    (``django_logic.E003`` territory — raised at class creation, #182).

    ``__getattr__`` only runs when normal attribute lookup *fails*, so a
    transition named ``state``, ``is_valid``, ``transitions``,
    ``process_name``… is unreachable: ``instance.process.is_valid()`` calls
    the bound method and returns ``True`` without transitioning anything.
    ``get_available_actions()`` still advertises the name, so the transition
    looks live and silently does nothing — the worst possible failure mode
    for a state machine. There is no way to reach it, so this is a
    definition error, not a runtime concern.
    """
    for proc_cls in _iter_process_tree(process_cls):
        for transition in proc_cls.transitions or []:
            action_name = getattr(transition, 'action_name', None)
            if not action_name:
                continue
            # The names that shadow __getattr__ are the class attributes UNION
            # the attributes __init__ sets on the instance. hasattr(cls, ...)
            # alone missed `state`, `instance` and `field_name` — and `state`
            # is the very first example this validator's own docstring cites.
            if not (action_name in _PROCESS_INSTANCE_ATTRS
                    or hasattr(proc_cls, action_name)):
                continue
            shadowed_by = (
                f'an attribute Process.__init__ sets ({action_name})'
                if action_name in _PROCESS_INSTANCE_ATTRS
                else repr(getattr(proc_cls, action_name))
            )
            raise ImproperlyConfigured(
                f"Process {proc_cls.__module__}.{proc_cls.__name__} declares "
                f"a transition named {action_name!r}, which is shadowed by "
                f"{shadowed_by}. Attribute lookup wins over the lazy action "
                f"dispatcher, so the transition could never be called — "
                f"rename the action."
            )


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
      Phase 1's transition resolution picks exactly one (the
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

    offenders = collect_hook_signature_offenders(process_cls)
    if not offenders:
        return
    message = _hook_signature_message(offenders)
    conf = getattr(settings, 'DJANGO_LOGIC', {}) or {}
    # Literal True only, same reasoning as STRICT_KWARGS_SERIALIZATION (#182).
    if conf.get('STRICT_HOOK_SIGNATURES', False) is True:
        raise ImproperlyConfigured(message)
    transition_logger.warning(message)


def _hook_signature_message(offenders) -> str:
    return (
        'FSM hooks without a named instance-first parameter — the engine '
        'calls hooks as fn(instance, **kwargs) (permissions as '
        'fn(instance, user, **kwargs)), so give each hook a named first '
        'parameter, e.g. def hook(instance, **kwargs); decorated hooks '
        'need functools.wraps to expose the real signature: '
        f'{"; ".join(sorted(set(offenders)))}'
    )


def collect_hook_signature_offenders(process_cls) -> list:
    """Every hook across ``process_cls``'s tree whose first parameter is not
    a named positional, as ``module.qualname (on Owner[.action])`` strings.
    Pure collection — enforcement lives in bind-time validation and the
    ``django_logic`` system check.
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
    return offenders


def _recovery_signature(transition, process_name: str) -> tuple:
    """What ``recover_stranded_states`` would do with this transition.

    Recovery is exactly ``fail_transition``, which reads only
    ``failed_state`` and the two failure bundles; everything else it
    touches is logging. Identity of the command objects, not equality —
    two equal-but-distinct callables compare as different, which errs
    toward calling a key ambiguous.

    ``process_name`` is the **bound** process's name, and it is part of
    the signature because the sweep's in-flight check is scoped by it
    (``_sweep_transition`` filters ``TransitionMessage`` on
    ``process_name``). A sibling process's open row is therefore
    invisible to this process's sweep: an instance legitimately mid-flight
    over there looks record-less here and would be force-failed. So two
    *different* bound processes claiming one in-progress state are always
    ambiguous, however identically they would recover — which is the
    cross-binding case #143 has always rejected.
    """
    return (
        process_name,
        transition.failed_state,
        tuple(id(command) for command in transition.failure_side_effects.commands),
        tuple(id(command) for command in transition.failure_callbacks.commands),
    )


def collect_ambiguous_in_progress_states() -> dict:
    """In-progress states whose stranded-recovery owner is undecidable
    (#143).

    Several transitions may share an ``in_progress_state`` on one
    (model, state_field) — declared side by side in a process tree, or
    reached through two *bindings*. That is only a problem for a
    **record-less** stranded instance: it has no provenance, so recovery
    has to pick an owner. Picking is safe when every claimant would
    recover identically (same ``failed_state``, same failure hooks) and
    unsafe otherwise, which is what this reports — not the sharing
    itself.

    Returns ``{(model_label, state_field, in_progress_state):
    [(process_cls, transition), ...]}`` for every key whose claimants
    disagree. ``Action``\\ s never write their ``in_progress_state`` and
    are excluded (mirrors the stranded sweep). Consumed by the
    ``django_logic.E001`` system check and by
    ``recover_stranded_states``, which skips ambiguous keys.
    """
    from django_logic.transition import Action

    claims: dict = {}
    # Signatures tracked alongside rather than recomputed from `owners`:
    # the bound process_name is part of a signature and is not recoverable
    # from (process_cls, transition) once a nested tree has been flattened.
    signatures: dict = {}
    for binding in ProcessManager.bindings:
        bound_process_name = binding.process_class.process_name
        for process_cls in _iter_process_tree(binding.process_class):
            for transition in process_cls.transitions or []:
                in_progress = getattr(transition, 'in_progress_state', None)
                if not in_progress or isinstance(transition, Action):
                    continue
                key = (binding.model._meta.label, binding.state_field,
                       in_progress)
                claims.setdefault(key, []).append((process_cls, transition))
                signatures.setdefault(key, set()).add(
                    _recovery_signature(transition, bound_process_name)
                )
    return {
        key: owners for key, owners in claims.items()
        if len(signatures[key]) > 1
    }


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
            # and registry entry are already in place (#143).
            return

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
        # runtime dispatch (#143).
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
        # holds, so check what would ACTUALLY be overwritten: any attribute
        # defined anywhere on the model's MRO. Asking _meta for field names
        # instead got this wrong in both directions — it flagged reverse
        # FK/M2M *query* names, which own no class attribute at all (so a
        # sound binding raised at import), and it missed every non-field
        # descriptor: a model method, a property, a cached_property, a
        # manager. `process` — the DEFAULT process_name — is a very plausible
        # method name on a model.
        clashing = next(
            (klass for klass in model.__mro__
             if process_class.process_name in vars(klass)),
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
            property(make_process_getter(state_field, process_class)),
        )
