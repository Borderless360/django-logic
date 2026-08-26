"""Transition — a single state-machine edge.

A ``Transition`` moves an instance from one of its source states to its
target state, running side-effects on success and either callbacks or
failure callbacks on completion. Everything happens synchronously, in
the caller's call frame — validate, lock, run, write the target state.

``target=None`` declares a transition that writes no state on success.
It runs under the same contract as every other transition: it takes the
state lock, it is refused while a background transition is uncompleted
(``TransitionTemporarilyUnavailable``), and it runs ``next_transition``.
A side-effect that must not obey that contract is not a transition —
write it as a plain method.

For background-executed transitions, see
``django_logic.background.BackgroundTransition``. That path has two
halves: enqueue (write ``in_progress_state`` + a ``TransitionMessage``
row in one transaction, then notify the workers) and execute (the
worker runs the side-effects and writes the final state). Definitions
live in ``django_logic.background.transitions`` (enqueue) and
``django_logic.background.runner`` (execute).
"""
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured

from django_logic.commands import (
    Callbacks,
    Conditions,
    NextTransition,
    Permissions,
    SideEffects,
    write_failed_state,
)
from django_logic.exceptions import (
    TransitionNotAllowed,
    TransitionTemporarilyUnavailable,
)
from django_logic.logger import (
    transition_logger,
    TransitionEventType,
)
from django_logic.state import State


#: Names of the engine's OWN method parameters on the state-change path.
#: A caller kwarg carrying one raises TypeError inside ``fail_transition``
#: — on the failure path, after the lock was taken: no ``failed_state``,
#: the real exception replaced, the lock leaked until TTL.
#: Refused up front, before anything is acquired. Distinct from the names
#: the engine forwards itself (``tr_id``, ``root_id``, ``parent_id``,
#: ``process_class``, ``owning_process_class``) — the README documents
#: those as reserved instead of refusing them.
_ENGINE_PARAM_KWARGS = frozenset({'state', 'exception'})


def _refuse_engine_param_kwargs(action_name: str, kwargs: dict) -> None:
    clashing = sorted(_ENGINE_PARAM_KWARGS & kwargs.keys())
    if clashing:
        raise TypeError(
            f"{action_name}() received {', '.join(repr(k) for k in clashing)}, "
            f"which name the engine's own parameters on the state-change path. "
            f"Passing them breaks the failure path (no failed_state, the real "
            f"exception replaced, and the state lock held until its TTL). "
            f"Rename the value, or nest it — e.g. "
            f"{action_name}(payload={{'exception': …}})."
        )


class Transition:
    """Synchronous transition from a source state to a target state.

    Execution order on success:
      1. lock state
      2. revalidate under the lock: the persisted state is still a valid
         source AND no background transition is in flight on this
         process (uncompleted ``TransitionMessage``)
      3. run side-effects
      4. on success: set ``target``, unlock, run callbacks, run ``next_transition``
      5. on failure: set ``failed_state`` (so failure hooks observe the
         contained state), unlock, run ``failure_callbacks`` (and re-raise)

    The state field does not change until the transition finishes:
    ``in_progress_state`` is background-only (0.12.0), where it is written
    atomically with the durable ``TransitionMessage`` row. A synchronous run
    that dies mid-run rolls back to its source state and can be run
    again — a visible "busy" step, where wanted, is a real state with an
    explicit fast transition into it, chained via ``next_transition``.
    """

    #: Swap points a consumer may subclass. Only these three — the
    #: failure-callback and condition bundles are always the stock classes.
    side_effects_class = SideEffects
    callbacks_class = Callbacks
    permissions_class = Permissions

    #: ``True`` on ``BackgroundTransition``. A public, stable attribute
    #: rather than an ``isinstance`` check: ``Process.__init_subclass__``
    #: reads it to enforce unique background action names without importing
    #: ``BackgroundTransition`` (which would close the cycle
    #: ``process → background.transitions → transition → process``), and
    #: consumers introspect it the same way.
    is_background: bool = False

    def __init__(
        self, action_name: str, sources: list, target: str | None = None,
        **kwargs,
    ):
        self.action_name = action_name
        # None (or '') means: write no state on success. Everything else
        # about the contract — lock, gate, chaining, failed_state — is
        # identical to a state-writing transition.
        self.target = target or None
        # Removed in 0.14.0; unknown kwargs are otherwise ignored, so a
        # declaration carrying one would silently lose behavior on upgrade.
        for removed, hint in (
            ('failure_side_effects', 'use failure_callbacks'),
            ('lock_timeout', "use the global DJANGO_LOGIC['LOCK_TIMEOUT']"),
        ):
            if removed in kwargs:
                raise ImproperlyConfigured(
                    f"Transition {action_name!r}: {removed}= was removed in "
                    f"0.14.0 — {hint}."
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
        self.in_progress_state = kwargs.get('in_progress_state')
        if self.in_progress_state and not self.is_background:
            # A durable busy marker needs a durable owner. Only a
            # background transition writes one (atomically with its
            # TransitionMessage row); the raise message says the rest.
            raise ImproperlyConfigured(
                f"Transition {action_name!r}: in_progress_state is only "
                f"supported on BackgroundTransition, where it is written "
                f"atomically with the durable TransitionMessage row. On a "
                f"synchronous transition that write is a record-less dead "
                f"end. Model the busy step as a real state with an "
                f"explicit transition into it, or make this transition a "
                f"BackgroundTransition."
            )
        if self.in_progress_state and not self.target:
            # Success writes no state, so nothing would ever move the
            # instance out of the in-progress state.
            raise ImproperlyConfigured(
                f"Transition {action_name!r}: in_progress_state needs a "
                f"target. With target=None success writes no state, so the "
                f"instance would stay parked in "
                f"{self.in_progress_state!r} after the work is done."
            )
        if self.in_progress_state and self.in_progress_state not in self.sources:
            # Treat the in-progress state as a valid source of the same
            # transition so the worker / retry paths can look the
            # transition up from an already-running instance. Visible
            # consequence: while a background transition is in progress
            # (instance in in_progress_state, enqueue lock already
            # released), the action still shows up in
            # get_available_actions() — the one-uncompleted-row gate is
            # enforced at invocation time (AlreadyInProgress), not at
            # listing time.
            self.sources.append(self.in_progress_state)
        self.failed_state = kwargs.get('failed_state')
        if self.failed_state and self.failed_state == self.in_progress_state:
            # The state field is what operators, UIs and the worker's
            # state guard read to tell "failed" from "still running";
            # identical, every failed instance is indistinguishable from
            # a busy one and the terminal write is a silent no-op.
            raise ImproperlyConfigured(
                f"Transition {action_name!r}: failed_state and "
                f"in_progress_state are both {self.failed_state!r}. A failed "
                f"instance would be indistinguishable from a running one, and "
                f"the terminal write a silent no-op. Give the failure its "
                f"own state."
            )
        # Only SideEffects dereferences its transition (to drive
        # complete/fail); the other command bundles never read it.
        self.failure_callbacks = Callbacks(
            kwargs.get('failure_callbacks', [])
        )
        self.side_effects = self.side_effects_class(
            kwargs.get('side_effects', []), transition=self
        )
        self.callbacks = self.callbacks_class(
            kwargs.get('callbacks', [])
        )
        self.permissions = self.permissions_class(
            kwargs.get('permissions', [])
        )
        self.conditions = Conditions(
            kwargs.get('conditions', [])
        )
        self.next_transition = NextTransition(kwargs.get('next_transition'))

    def __str__(self):
        if self.target is None:
            return f"Transition: {self.action_name} (no state write)"
        return f"Transition: {self.action_name} to {self.target}"

    def __repr__(self):
        return self.__str__()

    def is_valid(self, instance, user=None) -> bool:
        return (
            self.permissions.execute(instance, user)
            and self.conditions.execute(instance)
        )

    def change_state(self, state: State, **kwargs) -> UUID | None:
        # Before the lock: a clash here must not become a leaked lock.
        _refuse_engine_param_kwargs(self.action_name, kwargs)
        process_class = kwargs.get('process_class', '')
        process_class_name = process_class.split('.')[-1] if process_class else ''
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.START.value} '
            f'{process_class_name} {self.action_name} {state.instance_key} '
            f'{kwargs.get("root_id")} {kwargs.get("parent_id")}',
            # A copy, not the live dict: the caller mutates kwargs after
            # the log call, and records are formatted lazily.
            extra={'kwargs': dict(kwargs), 'state_hash': state._get_hash()},
        )

        # lock() is atomic (cache.add / Redis SET NX) and returns False if
        # the state is already locked, so the acquire alone is sufficient.
        # A separate is_locked() pre-check only adds a TOCTOU window and a
        # redundant round-trip (a stale is_locked()==True could even reject
        # a transition the atomic lock() would have granted).
        locked = state.lock()
        if not locked:
            # Logged BEFORE the raise, or a permanently frozen instance is
            # indistinguishable from a healthy start: both emit one Start line
            # and nothing else. INFO, not ERROR: losing the lock race is an
            # expected concurrency outcome; it is the *pattern* of failed
            # acquisitions with no interleaved Unlock that signals a leak.
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.LOCK.value} '
                f'failed {state.instance_key} — state is locked'
            )
            raise TransitionNotAllowed("State is locked")

        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.LOCK.value} '
            f'{state.instance_key}'
        )

        # Revalidate under the lock. The source/condition checks in
        # the transition was resolved before the lock was acquired;
        # by now a concurrent transition may have won the race and moved
        # the state (validate-then-lock TOCTOU). One cheap query closes it.
        # Any failure here must release the lock or the instance's FSM
        # freezes until the lock TTL expires.
        try:
            self._ensure_db_state_in_sources(state)
            self._ensure_no_background_in_flight(state)
        except Exception:
            state.unlock()
            # Without this line the per-instance lifecycle shows a Lock
            # with no Unlock — a revalidation failure reading as a leak.
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.UNLOCK.value} '
                f'{state.instance_key} after revalidation failure'
            )
            raise

        self._init_transition_context(kwargs)
        self.side_effects.execute(state, **kwargs)
        return kwargs.get('tr_id')

    def complete_transition(self, state: State, **kwargs):
        """Write the target state (when one is declared), release the
        lock, then run callbacks.

        The lock is released **before** callbacks run, so a callback can
        safely trigger another transition on the same instance. If the
        worker crashes during callbacks they are lost — callbacks are
        best-effort.

        A failed target write must still release the lock (otherwise the
        instance's FSM freezes until the lock TTL): the transition fails
        loudly either way, but a leaked lock turns one failed request into
        hours of rejected transitions.
        """
        if self.target is not None:
            try:
                state.set_state(self.target)
            except Exception:
                transition_logger.error(
                    f'{kwargs.get("tr_id")} target-state write failed for '
                    f'{state.instance_key}; releasing the lock before re-raising.'
                )
                self._release_lock(state, **kwargs)
                raise
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
                f'{self.target}'
            )

        self._release_lock(state, **kwargs)

        self.callbacks.execute(state, **kwargs)
        self.next_transition.execute(state, **kwargs)

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        # try/finally: a failed failed_state write must still release the
        # lock; the original side-effect exception keeps propagating out of
        # SideEffects.execute either way.
        try:
            if self.failed_state:
                # Savepointed so a rejected failed_state write cannot replace
                # the original side-effect exception on its way out. The
                # docstring above promised "the original exception keeps
                # propagating either way"; without this the write's own
                # exception won and the real cause was lost.
                write_failed_state(
                    state, self.failed_state,
                    prefix=f'{kwargs.get("tr_id")}',
                    consequence='The original failure is re-raised unchanged.',
                )
        finally:
            self._release_lock(state, **kwargs)

        self.failure_callbacks.execute(state, exception=exception, **kwargs)

    @staticmethod
    def _release_lock(state: State, **kwargs):
        """Release the state lock and log the Unlock lifecycle line."""
        state.unlock()
        # instance_key on the lifecycle lines: Start used to be the only
        # line carrying it, so a per-instance log filter could not show
        # whether the lock was ever taken or released — the absence of a
        # Lock line was invisible without a tr_id self-join.
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.UNLOCK.value} '
            f'{state.instance_key}'
        )

    @staticmethod
    def _init_transition_context(kwargs: dict) -> None:
        kwargs.setdefault('context', {})

    def _ensure_db_state_in_sources(self, state: State) -> None:
        """Re-read the persisted state and verify it is still a valid
        source for this transition. Must be called while holding the lock.
        """
        db_state = state.get_persisted_state()
        if db_state not in self.sources:
            raise TransitionNotAllowed(
                f"Transition '{self.action_name}' is not allowed: the "
                f"persisted state {db_state!r} is no longer one of its "
                f"source states (a concurrent transition won the race)."
            )

    def _ensure_no_background_in_flight(self, state: State) -> None:
        """Reject any synchronous transition — with a target or without
        one — while a background transition is in progress on the same
        instance + process.

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
        with enqueue's constraint rejection and the public probe.
        """
        from django.apps import apps

        if not apps.is_installed('django_logic'):
            return
        from django_logic.background.models import TransitionMessage

        status = TransitionMessage.retry_status(
            state.instance, state.process_name)
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
                f"or complete the row."
            )
        raise TransitionTemporarilyUnavailable(
            f"Transition '{self.action_name}' is not allowed right now: "
            f"a background transition is in progress for "
            f"{state.instance_key} (uncompleted TransitionMessage)."
        )
