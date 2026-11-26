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
from functools import cache, wraps

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured

from django_logic.commands import Conditions, Permissions
from django_logic.exceptions import (
    RefusalReason,
    TransitionNotAllowed,
    _check_validity,
    _execute_validity_bundle,
    _record_validity_refusal,
)
from django_logic.logger import transition_logger
from django_logic.state import State


# Per-execution-chain context that propagates transition metadata
# (root_id, tr_id) through nested callbacks without explicit kwargs forwarding.
_transition_context: ContextVar[dict | None] = ContextVar(
    '_transition_context', default=None
)

_DEFAULT_REFUSAL_MESSAGES = {
    RefusalReason.PERMISSION: 'you do not have permission to perform this action.',
    RefusalReason.CONDITION: 'this record does not meet the requirements for this action.',
    RefusalReason.SOURCE_STATE: 'this action cannot run in the current state of this record.',
    RefusalReason.LOCKED: 'this record is locked.',
    RefusalReason.BACKGROUND_IN_FLIGHT: 'a background transition is in progress. Try again shortly.',
    RefusalReason.BACKGROUND_STRANDED: (
        'a background transition for this record is stranded. Please contact support.'),
    RefusalReason.AMBIGUOUS: 'the action could not be resolved. Please contact support.',
}
_Refusal = namedtuple('_Refusal', 'reason message process transition')

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
    ``process_name``, ``state_class``, ``permissions_class`` and ``refusal_messages``.

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
    refusal_messages = {}

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
        if item.startswith('_') or item not in _declared_action_names(type(self)):
            raise AttributeError(item)

        def transition_method(**kwargs):
            return self._get_transition_method(item, **kwargs)

        transition_method.__name__ = item
        transition_method.__qualname__ = item
        # A template must not run a transition while rendering its callable.
        transition_method.alters_data = True

        return transition_method

    def _get_transition_method(self, action_name: str, /, **kwargs):
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
            else "(no state write, no lock)" if not transition.lock
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
        kwargs['process_class'] = (
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )
        kwargs['owning_process_class'] = (
            f"{type(owning_process).__module__}.{type(owning_process).__name__}"
        )

        token = _transition_context.set(
            {'root_id': kwargs['root_id'], 'tr_id': kwargs['tr_id']}
        )
        try:
            return transition.change_state(self.state, **kwargs)
        except TransitionNotAllowed as error:
            self._set_refusal_message(error, action_name, owning_process, transition)
            raise
        finally:
            _transition_context.reset(token)

    def _set_refusal_message(
        self, error, action_name, process=None, transition=None, message=None,
    ):
        if error.user_message is not None:
            return
        reason = error.reason
        if message is None:
            maps = [transition.refusal_messages] if transition is not None else []
            if process is not None and process is not self:
                maps.append(process.refusal_messages)
            maps.extend([self.refusal_messages, _DEFAULT_REFUSAL_MESSAGES])
            message = next((mapping[reason] for mapping in maps if reason in mapping), None)
        outcome = (
            'temporarily unavailable'
            if reason == RefusalReason.BACKGROUND_IN_FLIGHT else 'not allowed'
        )
        prefix = f"Action '{action_name}' is {outcome}"
        error.user_message = prefix if message is None else f'{prefix}: {message}'

    def is_valid(self, user=None) -> bool:
        permissions = self.permissions_class(commands=self.permissions)
        conditions = Conditions(commands=self.conditions)
        instance = self.state.instance
        permitted = _execute_validity_bundle(self, permissions, instance, user)
        if not permitted:
            _record_validity_refusal(self, RefusalReason.PERMISSION)
            return permitted
        valid = _execute_validity_bundle(self, conditions, instance)
        if not valid:
            _record_validity_refusal(self, RefusalReason.CONDITION)
        return valid

    def get_available_actions(self, user=None, action_name=None, *, include_locked=False):
        """List eligible actions, optionally including those hidden by a held state lock."""
        return sorted(
            {
                transition.action_name
                for transition in self.get_available_transitions(
                    user, action_name, include_locked=include_locked)
            }
        )

    def get_available_transitions(self, user=None, action_name=None, *, include_locked=False):
        """Check current state, conditions and permissions without acquiring a lock.

        ``include_locked`` changes only this query. Calling the action checks conditions and permissions before locking.
        Under the lock, execution checks persisted state and conflicting unfinished work again.
        """
        for transition, _owner in self._iter_available_with_owner(
            user=user,
            action_name=action_name,
            include_locked=include_locked,
        ):
            yield transition

    def _iter_available_with_owner(
        self,
        user=None,
        action_name=None,
        include_locked=False,
        _seen=None,
        _refusals=None,
        _is_locked=None,
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

        if _refusals is None:
            valid, reason, message = self.is_valid(user), None, None
        else:
            valid, reason, message = _check_validity(self, user)
        if not valid:
            if _refusals is not None:
                declarations = [
                    transition
                    for process_class in _iter_process_tree(
                        type(self), _seen=_seen - {id(type(self))},
                    )
                    for transition in process_class.transitions
                    if transition.action_name == action_name
                ]
                if any(
                    self.state.get_state() in transition.sources
                    for transition in declarations
                ):
                    _refusals['process'].append(_Refusal(reason, message, self, None))
                elif declarations:
                    _refusals['source'].append(_Refusal(RefusalReason.SOURCE_STATE, None, self, None))
            return

        # A held lock hides the transitions that would take it. A lock=False
        # transition runs regardless, so it stays listed.
        # Nested processes share this state, so one listing reads its lock once.
        if _is_locked is None:
            _is_locked = cache(self.state.is_locked)
        locked = not include_locked and _is_locked()

        for transition in self.transitions:
            if action_name is not None and transition.action_name != action_name:
                continue
            if locked and transition.lock:
                continue

            if self.state.get_state() not in transition.sources:
                if _refusals is not None:
                    _refusals['source'].append(_Refusal(RefusalReason.SOURCE_STATE, None, self, transition))
                continue
            if _refusals is None:
                valid, reason, message = transition.is_valid(self.state.instance, user), None, None
            else:
                valid, reason, message = _check_validity(transition, self.state.instance, user)
            if valid:
                yield transition, self
            elif _refusals is not None:
                _refusals['transition'].append(_Refusal(reason, message, self, transition))

        for sub_process_class in self.nested_processes:
            sub_process = sub_process_class(state=self.state)
            yield from sub_process._iter_available_with_owner(
                user=user,
                action_name=action_name,
                include_locked=include_locked,
                _seen=_seen,
                _refusals=_refusals,
                _is_locked=_is_locked,
            )

    def _resolve_transition_with_owner(self, action_name: str, user=None):
        """Resolve ``action_name`` to ``(transition, owning_process)``.

        Exactly one match is required, after conditions/permissions
        filtering with ``include_locked=True``. Also returns the declaring
        process so the caller can record it for worker restore.
        """
        # Prefer a checked transition over a different branch's process guard.
        refusals = {'transition': [], 'process': [], 'source': []}
        matches = list(
            self._iter_available_with_owner(
                action_name=action_name,
                user=user,
                include_locked=True,
                _refusals=refusals,
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
            error = TransitionNotAllowed(
                "There are several transitions available",
                reason=RefusalReason.AMBIGUOUS,
            )
            self._set_refusal_message(error, action_name)
            raise error

        current_state = self.state.get_state()
        message = (
            f"Process class {self.__class__} for object "
            f"{self.state.instance.pk} has no transition "
            f"with action name {action_name}, user {user}. "
            f"The instance is in state {current_state!r}."
        )
        transition_logger.info(message)
        refusal = next(
            (reasons[0] for reasons in refusals.values() if reasons),
            None,
        )
        if refusal is None:
            raise AttributeError(action_name)
        error = TransitionNotAllowed(message, reason=refusal.reason)
        self._set_refusal_message(error, action_name, refusal.process, refusal.transition, refusal.message)
        error.current_state = current_state
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
    for sub_process_cls in process_cls.nested_processes:
        yield from _iter_process_tree(sub_process_cls, _seen)


@cache
def _declared_action_names(process_class):
    return frozenset(transition.action_name for owner in _iter_process_tree(process_class)
                     for transition in owner.transitions)


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
        for transition in proc_cls.transitions:
            action_name = transition.action_name
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
    def _where(proc_cls, index):
        return f"{proc_cls.__module__}.{proc_cls.__name__}.transitions[{index}]"

    for proc_cls in _iter_process_tree(process_cls):
        local_background: dict[str, str] = {}
        for index, transition in enumerate(proc_cls.transitions):
            if not transition.is_background:
                continue
            name = transition.action_name
            if name in local_background:
                raise ImproperlyConfigured(
                    f"Process {process_cls.__module__}."
                    f"{process_cls.__name__} (or its nested processes) "
                    f"has two background transitions sharing "
                    f"action_name='{name}' within a single process class "
                    f"({local_background[name]} and "
                    f"{_where(proc_cls, index)}). The worker restores "
                    f"a background transition by (declaring class, "
                    f"action_name), so background action_names must be "
                    f"unique within a process class. Move one to a "
                    f"separate nested process or rename it."
                )
            local_background[name] = _where(proc_cls, index)


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
    offenders, request_readers = [], []
    for fn, owner in _iter_hooks(process_cls):
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        name = f'{getattr(fn, "__module__", "?")}.{getattr(fn, "__qualname__", fn)} (on {owner})'
        first = next(iter(params.values()), None)
        if first is None or first.kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            offenders.append(name)
        if 'request' in params:
            request_readers.append(name)
    if offenders:
        raise ImproperlyConfigured(
            'Transition hooks without a named instance-first parameter. '
            'The engine calls conditions as fn(instance), permissions as '
            'fn(instance, user), and side effects and callbacks as '
            'fn(instance, **kwargs). Failure callbacks also receive exception '
            'as a keyword argument. Give each hook a named first parameter; decorated hooks '
            'need functools.wraps to expose the real signature: '
            f'{"; ".join(sorted(set(offenders)))}'
        )
    if request_readers:
        raise ImproperlyConfigured(
            'Transition hooks that name a request parameter — a transition never '
            'takes the request (it is refused at the call, and a worker has '
            'none), so a hook can never receive one. Resolve what the hook '
            'needs at the call site and pass plain values: '
            f'{"; ".join(sorted(set(request_readers)))}'
        )


def _iter_hooks(process_cls):
    """Every declared hook in the process tree with the owner named in validation errors."""
    hook_attrs = (
        'side_effects', 'callbacks', 'failure_callbacks',
        'conditions', 'permissions',
    )
    for proc_cls in _iter_process_tree(process_cls):
        owner = f'{proc_cls.__module__}.{proc_cls.__name__}'
        # Computed properties cannot be checked before an instance exists.
        for attr in ('conditions', 'permissions'):
            hooks = getattr(proc_cls, attr)
            if isinstance(hooks, (list, tuple)):
                for fn in hooks:
                    yield fn, owner
        for transition in proc_cls.transitions:
            for attr in hook_attrs:
                wrapper = getattr(transition, attr)
                for fn in wrapper.commands:
                    yield fn, f'{owner}.{transition.action_name}'


#: One record per ``bind_model_process`` call.
ModelProcessBinding = namedtuple(
    'ModelProcessBinding', ['model', 'process_class', 'state_field'])


# Only model.save() opts in. Raw fixture writes and explicit field lists keep Django's behavior.
_ordinary_save = ContextVar('django_logic_ordinary_save', default=None)


@cache
def _protect_model_save(model):
    # Proxies and multi-table children can write the same column through its declaring model.
    original_save = model.save
    original_save_base = model.save_base
    original_update = model._do_update

    @wraps(original_save)
    def save(instance, *args, **kwargs):
        # Django 5.2 also accepts update_fields as its fourth positional argument.
        update_fields = args[3] if len(args) >= 4 else kwargs.get('update_fields')
        token = _ordinary_save.set(instance if update_fields is None else None)
        try:
            return original_save(instance, *args, **kwargs)
        finally:
            _ordinary_save.reset(token)

    @wraps(original_save_base)
    def save_base(instance, raw=False, force_insert=False, force_update=False, using=None, update_fields=None):
        if (_ordinary_save.get() is instance and not raw and not force_insert
                and not instance._state.adding and using == instance._state.db
                and instance._is_pk_set() and any(
                    not field.generated and field.attname not in instance.__dict__
                    for field in instance._meta.concrete_fields
                )):
            # Django has now included fields assigned by a custom save() and used the names its signals expect.
            protected = ProcessManager.state_fields(type(instance))
            update_fields = frozenset(
                name for name in update_fields or () if instance._meta.get_field(name).name not in protected
            )
            if not update_fields:
                return
        return original_save_base(instance, raw=raw, force_insert=force_insert, force_update=force_update,
                                  using=using, update_fields=update_fields)

    @wraps(original_update)
    def update(instance, base_qs, using, pk_val, values, update_fields, *args, **kwargs):
        # Django may try an update before an insert, even for an instance with a new primary key.
        # Exclude state only from the update. An insert must keep the initial values.
        if update_fields is None and _ordinary_save.get() is instance:
            protected = ProcessManager.state_fields(type(instance))
            values = [value for value in values if value[0].name not in protected]
        return original_update(instance, base_qs, using, pk_val, values, update_fields, *args, **kwargs)

    model.save = save
    model.save_base = save_base
    model._do_update = update


class ProcessManager:
    #: Public registry of every bound machine, in bind order. Consumer
    #: tooling (coverage audits, contract tests, the ``django_logic``
    #: system check) reads this instead of re-deriving bindings from
    #: model attributes.
    bindings: list = []

    @classmethod
    @cache
    def state_fields(cls, model):
        """Return the names of process state fields stored by this model."""
        bound_fields = {
            binding.model._meta.get_field(binding.state_field) for binding in cls.bindings
        }
        return frozenset(field.name for field in model._meta.concrete_fields if field in bound_fields)

    @classmethod
    def bind_model_process(cls, model, process_class, state_field: str = 'state') -> None:
        binding = ModelProcessBinding(model, process_class, state_field)
        if binding in cls.bindings:
            # Identical re-bind (an AppConfig.ready() running twice, a
            # test re-import) is a harmless no-op — the model property
            # and registry entry are already in place.
            return

        from django.apps import apps as django_apps

        if not django_apps.is_installed('django_logic'):
            raise ImproperlyConfigured(
                "Process binding requires 'django_logic' in INSTALLED_APPS. Add it and run manage.py migrate."
            )

        # Refuse an invalid state field here, before a later transition tries to write it.
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

        # A different binding under the same name would leave system checks inspecting a process the model no longer calls.
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
        cls.state_fields.cache_clear()
        _protect_model_save(field.model)

        def make_process_getter(field_name, process_cls):
            return lambda self: process_cls(field_name=field_name, instance=self)

        setattr(
            model,
            process_class.process_name,
            _ProcessAccessor(make_process_getter(state_field, process_class)),
        )
