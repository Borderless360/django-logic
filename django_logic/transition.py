"""Transition — a single state-machine edge.

A ``Transition`` moves an instance from one of its source states to its
target state, running side-effects on success and either callbacks or
failure callbacks on completion. Everything happens synchronously, in
the caller's call frame — validate, lock, run, write the target state.

``target=None`` declares a transition that writes no state on success.
It runs under the same contract as every other transition: it takes the
state lock, it is refused while a background transition is uncompleted
(``TransitionTemporarilyUnavailable``), and it runs callbacks after success.

``lock=False`` declares a transition that also takes no state lock. It
is for a side effect whose unit of work is not the bound row — a store
posting one parcel's tracking, where the row is the store and the work
is the parcel. Several may run at once on one instance, and one runs
while a background transition on that instance is uncompleted. It keeps
conditions, permissions, side effects, callbacks and failure callbacks.
It may not declare a ``target`` or a ``failed_state``: a state write must
serialise on that state. It is refused on ``BackgroundTransition``, whose
enqueue writes a row and a busy state under that lock.

For background-executed transitions, see
``django_logic.background.BackgroundTransition``. That path has two
halves: enqueue (write ``in_progress_state`` + a ``TransitionMessage``
row in one transaction, then notify the workers) and execute (the
worker runs the side-effects and writes the final state). Definitions
live in ``django_logic.background.transitions`` (enqueue) and
``django_logic.background.runner`` (execute).
"""
from functools import partial
from contextlib import contextmanager
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, transaction

from django_logic.commands import (
    guard_failure,
    Callbacks,
    Conditions,
    Permissions,
    SideEffects,
    write_failed_state, reload_after_rollback,
)
from django_logic.exceptions import (
    RefusalReason,
    TransitionNotAllowed,
    TransitionTemporarilyUnavailable,
)
from django_logic.logger import (
    transition_logger,
    TransitionEventType,
)
from django_logic.state import State


#: Refuse argument names that collide with engine parameters before taking locks.
#: ``state`` collides during side-effect execution; ``exception`` collides during failure handling.
#: The README's "Run a transition" section lists engine-managed arguments.
_ENGINE_PARAM_KWARGS = frozenset({'state', 'exception'})

def _refuse_engine_param_kwargs(action_name: str, kwargs: dict) -> None:
    if 'request' in kwargs:
        raise TypeError(
            f"{action_name}() received 'request'. A transition never takes "
            f"the request: hooks are fn(instance, **kwargs) and run on a "
            f"worker for a background transition, where no request exists. "
            f"Resolve what you need at the call site and pass plain values "
            f"— the engine already carries user= and restores it on the "
            f"worker."
        )
    clashing = sorted(_ENGINE_PARAM_KWARGS & kwargs.keys())
    if clashing:
        raise TypeError(
            f"{action_name}() received {', '.join(repr(k) for k in clashing)}, "
            f"which name the engine's own parameters on the state-change path. "
            f"Passing them breaks the failure path (no failed_state, the real "
            f"exception replaced, and a missing failure report). "
            f"Rename the value, or nest it — e.g. "
            f"{action_name}(payload={{'exception': …}})."
        )


class Transition:
    """One action whose required changes commit together on the record database."""

    #: ``True`` on ``BackgroundTransition``. A public, stable attribute
    #: rather than an ``isinstance`` check: ``Process.__init_subclass__``
    #: reads it to enforce unique background action names without importing
    #: ``BackgroundTransition`` (which would close the cycle
    #: ``process → background.transitions → transition → process``), and
    #: consumers introspect it the same way.
    is_background: bool = False

    def __init__(
        self, action_name: str, sources: list, target: str | None = None,
        *, lock: bool = True,
        conditions=(), permissions=(), side_effects=(), callbacks=(),
        failure_callbacks=(), failed_state=None, refusal_messages=None,
    ):
        self.action_name = action_name
        self.refusal_messages = {} if refusal_messages is None else refusal_messages
        # None means: write no state on success. An empty string is a state. Everything else
        # about the contract — lock, unfinished-job check, failed_state — is
        # identical to a state-writing transition, unless lock=False.
        self.target = target
        self.lock = bool(lock)
        if not self.lock and self.target is not None:
            raise ImproperlyConfigured(
                f"Transition {action_name!r}: lock=False needs target=None. "
                f"A transition that writes {self.target!r} must serialise on "
                f"that state, or two callers race the write."
            )
        if isinstance(sources, str):
            # list('draft') is ['d','r','a','f','t'], which matches no state:
            # the transition becomes invisible to get_available_actions() and
            # calling it reports a missing action rather than a bad
            # declaration. Fail at declaration time instead.
            raise ImproperlyConfigured(
                f"Transition {action_name!r}: sources must be a list of "
                f"states, not the bare string {sources!r} — a string is "
                f"iterated per character. Use sources=[{sources!r}]."
            )
        self.sources = list(sources)
        self.in_progress_state = None
        self.failed_state = failed_state
        if self.failed_state and not self.lock:
            raise ImproperlyConfigured(
                f"Transition {action_name!r}: lock=False cannot declare a "
                f"failed_state. Writing {self.failed_state!r} on failure is a "
                f"state write, and a state write must serialise on the lock. "
                f"Record the failure in a failure_callback instead."
            )
        self.failure_callbacks = Callbacks(failure_callbacks)
        self.side_effects = SideEffects(side_effects, transition=self)
        self.callbacks = Callbacks(callbacks)
        self.permissions = Permissions(permissions)
        self.conditions = Conditions(conditions)

    def __str__(self):
        if not self.lock:
            return f"Transition: {self.action_name} (no state write, no lock)"
        if self.target is None:
            return f"Transition: {self.action_name} (no state write)"
        return f"Transition: {self.action_name} to {self.target}"

    def __repr__(self):
        return self.__str__()

    def _guard_failure(self, instance, user):
        return guard_failure(self.permissions, self.conditions, instance, user)

    def is_valid(self, instance, user=None) -> bool:
        return self._guard_failure(instance, user) is None

    def _current_call(self, state):
        from django_logic.process import _transition_context
        call = (_transition_context.get() or {}).get('call')
        if call is not None and call['state'] is state and call['transition'] is self and not call.get('entered'):
            return call
        return None

    @contextmanager
    def _execution_scope(self, state, user, call, tr_id):
        if call is not None:
            # A subclass can call another transition directly. Only this invocation owns the dispatch lock.
            with state.executing_action():
                transition_logger.info(f'{tr_id} {TransitionEventType.LOCK.value} {state.instance_key}')
                call['entered'] = True
                try:
                    yield
                finally:
                    call.pop('entered', None)
                    transition_logger.info(f'{tr_id} {TransitionEventType.UNLOCK.value} {state.instance_key}')
            return
        if not self.lock:
            with transaction.atomic(using=state.instance._state.db):
                yield
            return
        with state.protected() as fresh:
            if getattr(fresh, state.field_name) not in self.sources:
                raise TransitionNotAllowed('The persisted state no longer permits this action.', reason=RefusalReason.SOURCE_STATE)
            if not self.is_valid(fresh, user):
                raise TransitionNotAllowed('The record no longer permits this action.')
            with state.executing_action():
                yield

    def change_state(self, state: State, **kwargs) -> UUID | None:
        _refuse_engine_param_kwargs(self.action_name, kwargs)
        using = state.instance._state.db or DEFAULT_DB_ALIAS
        call = self._current_call(state)
        run_callbacks = True
        error = None
        with self._execution_scope(state, kwargs.get('user'), call, kwargs.get('tr_id')):
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.START.value} '
                f'{kwargs.get("process_class", "").split(".")[-1]} {self.action_name} {state.instance_key} '
                f'{kwargs.get("root_id")} {kwargs.get("parent_id")}',
                extra={'kwargs': dict(kwargs), 'state_hash': state._get_hash()},
            )
            if self.lock:
                self._ensure_no_background_in_flight(state)
            else:
                transition_logger.info(f'{kwargs.get("tr_id")} {TransitionEventType.LOCK.value} skipped {state.instance_key} — lock=False')
            self._init_transition_context(kwargs)
            try:
                self.side_effects.execute(state, **kwargs)
            except Exception as exception:
                # Commit the failure state before reporting the original failure to the caller.
                error = exception
                run_callbacks = reload_after_rollback(state, prefix=kwargs.get('tr_id'))
                if run_callbacks:
                    run_callbacks = self.fail_transition(state, error, **kwargs) is not False
                if call is not None:
                    call['failure'] = exception
        callbacks = self.callbacks if error is None else self.failure_callbacks
        if run_callbacks and callbacks.commands:
            if error is not None:
                kwargs['exception'] = error
            register = partial(callbacks.after_commit, state, using, kwargs)
            if call is not None:
                call['after'].append(register)
            else:
                register()
        if error is not None:
            raise error
        return kwargs.get('tr_id')

    def complete_transition(self, state: State, **kwargs):
        """Write the target, then let subclasses save required follow-up work in this transaction."""
        self._write_target_state(state, **kwargs)
        if self.lock:
            state.finish_action()

    def _write_target_state(self, state: State, **kwargs):
        if self.target is not None:
            state.set_state(self.target)
            from django_logic.background.runner import _active_job
            job = _active_job.get()
            if job is not None:
                job.results[(state.instance_key, state.process_name)] = self.target
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
                f'{self.target}'
            )

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        if self.failed_state:
            write_error = write_failed_state(
                state, self.failed_state, prefix=kwargs.get('tr_id'),
                consequence='The original failure is re-raised unchanged.',
            )
            if write_error is not None:
                return reload_after_rollback(state, prefix=kwargs.get('tr_id'))

    @staticmethod
    def _init_transition_context(kwargs: dict) -> None:
        kwargs.setdefault('context', {})

    def _ensure_no_background_in_flight(self, state: State) -> None:
        """Reject any lock-taking synchronous transition — with a target or
        without one — while a background transition is in progress on the
        same instance + process. A ``lock=False`` transition is not checked:
        it touches no state, so it has nothing to interleave with.

        Without this gate a synchronous transition could interleave with
        the worker: a target write would race the worker's state writes,
        and side-effects would run against a row mid-change.
        Checked under the lock, like the source revalidation.

        A row that is still being retried raises the transient type: it
        clears when that work completes, so "come back in a moment" is
        the right answer. A stranded row raises the plain base: nothing
        is retrying it, so "retry shortly" would be wrong forever, and
        hook-path logging must stay at ERROR — a stuck instance pages.
        The classification (``TransitionMessage.retry_status``) is shared
        with enqueue's unfinished-job check and the public probe.
        """
        from django_logic.background.models import TransitionMessage

        from django_logic.background.runner import _active_job

        job = _active_job.get()
        status = TransitionMessage.retry_status(
            state.instance, state.process_name, excluding_job=job.pk if job else None)

        if status is None:
            return
        if status == TransitionMessage.STRANDED:
            raise TransitionNotAllowed(
                f"Transition '{self.action_name}' is not allowed: a "
                f"background transition for {state.instance_key} has an "
                f"uncompleted TransitionMessage that is stranded — "
                f"nothing is retrying it. Likely causes: no worker serves "
                f"its queue, or a worker outage longer than the retry "
                f"window. Start a worker for that queue "
                f"(dl_worker --queues ...) — it takes the row at once — "
                f"or complete the row.",
                reason=RefusalReason.BACKGROUND_STRANDED,
            )
        raise TransitionTemporarilyUnavailable(
            f"Transition '{self.action_name}' is not allowed right now: "
            f"a background transition is in progress for "
            f"{state.instance_key} (uncompleted TransitionMessage).",
            reason=RefusalReason.BACKGROUND_IN_FLIGHT,
        )
