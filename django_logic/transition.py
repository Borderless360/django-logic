"""Transition — a single state-machine edge.

A ``Transition`` moves an instance from one of its source states to its
target state, running side-effects on success and either callbacks or
failure callbacks on completion. Everything happens synchronously, in
the caller's call frame — validate, lock, run, write the target state.

``Action`` is a transition that does not change state on success but
still runs side-effects and can set a ``failed_state`` on failure.

For background-executed transitions, see
``django_logic.background.BackgroundTransition``. Comments in this
module that mention "phase 1" / "phase 2" refer to the two halves of a
*background* transition (the transactional-outbox pattern): phase 1 is
the synchronous part that durably records the intent (write
``in_progress_state`` + a ``TransitionMessage`` row in one transaction,
then enqueue the Celery task), phase 2 is the worker-side part that
executes the side-effects and writes the final state. Definitions live
in ``django_logic.background.transitions`` (phase 1) and
``django_logic.background.runner`` (phase 2).
"""
import math
from abc import ABC
from uuid import UUID

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, transaction

from django_logic.commands import (
    Callbacks,
    Conditions,
    FailureSideEffects,
    NextTransition,
    Permissions,
    SideEffects,
)
from django_logic.exceptions import TransitionNotAllowed
from django_logic.logger import (
    redact_log_kwargs,
    transition_logger,
    TransitionEventType,
)
from django_logic.state import State


def _defer_unlock_until_commit() -> bool:
    """DJANGO_LOGIC['DEFER_UNLOCK_UNTIL_COMMIT'] — read on every call
    (not cached at import time), like LOCK_TIMEOUT."""
    return bool(
        getattr(django_settings, 'DJANGO_LOGIC', {})
        .get('DEFER_UNLOCK_UNTIL_COMMIT', False)
    )


class BaseTransition(ABC):
    side_effects_class = SideEffects
    callbacks_class = Callbacks
    failure_callbacks_class = Callbacks
    failure_side_effects_class = FailureSideEffects
    permissions_class = Permissions
    conditions_class = Conditions
    next_transition_class = NextTransition

    # Class-level marker; overridden to ``True`` by ``BackgroundTransition``.
    #
    # Used by ``Process.__init_subclass__`` to enforce unique background
    # ``action_name``s without importing ``BackgroundTransition`` (that
    # would create a circular: ``process → background.transitions →
    # transition → process``). Kept as a stable public attribute so third
    # parties can introspect a transition without caring about concrete
    # subclasses (e.g. admin tooling, code-gen). Duck-typed by design;
    # ``isinstance(..., BackgroundTransition)`` is equivalent but more
    # coupling-heavy for callers.
    is_background: bool = False

    def is_valid(self, instance, user=None) -> bool:
        raise NotImplementedError

    def change_state(self, state: State, **kwargs):
        raise NotImplementedError

    def complete_transition(self, state: State, **kwargs):
        raise NotImplementedError

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        raise NotImplementedError


class Transition(BaseTransition):
    """Synchronous transition from a source state to a target state.

    Execution order on success:
      1. lock state
      2. revalidate under the lock: the persisted state is still a valid
         source AND no background transition is in flight on this
         process (uncompleted ``TransitionMessage``)
      3. optionally set ``in_progress_state``
      4. run side-effects
      5. on success: set ``target``, unlock, run callbacks, run ``next_transition``
      6. on failure: set ``failed_state`` (so failure hooks observe the
         contained state), run ``failure_side_effects``, unlock, run
         ``failure_callbacks`` (and re-raise)
    """

    def __init__(self, action_name: str, sources: list, target: str, **kwargs):
        self.action_name = action_name
        self.target = target
        self.sources = list(sources)
        self.in_progress_state = kwargs.get('in_progress_state')
        if self.in_progress_state and self.in_progress_state not in self.sources:
            # Treat the in-progress state as a valid source of the same
            # transition so phase 2 / retry paths can look the transition
            # up from an already-in-flight instance. Visible consequence:
            # while a background transition is in flight (instance in
            # in_progress_state, phase-1 lock already released), the action
            # still shows up in get_available_actions() — the one-in-flight
            # gate is enforced at invocation time (AlreadyInProgress), not
            # at listing time.
            self.sources.append(self.in_progress_state)
        self.failed_state = kwargs.get('failed_state')
        # Per-transition override of the global LOCK_TIMEOUT for the
        # synchronous execution path — for transitions whose side-effects
        # legitimately run long (report generation, large exports). The
        # lock is the liveness signal recover_stranded_states relies on,
        # so size it above the longest expected run. Background
        # transitions don't need this: their phase-1 critical section is
        # short and their in-flight marker is the TransitionMessage row.
        self.lock_timeout = kwargs.get('lock_timeout')
        if self.lock_timeout is not None and (
            not isinstance(self.lock_timeout, (int, float))
            or isinstance(self.lock_timeout, bool)
            or self.lock_timeout <= 0
            or not math.isfinite(self.lock_timeout)
        ):
            raise ImproperlyConfigured(
                f"Transition '{action_name}': lock_timeout must be a "
                f"positive number of seconds, got {self.lock_timeout!r}."
            )
        self.failure_callbacks = self.failure_callbacks_class(
            kwargs.get('failure_callbacks', []), transition=self
        )
        self.failure_side_effects = self.failure_side_effects_class(
            kwargs.get('failure_side_effects', []), transition=self
        )
        self.side_effects = self.side_effects_class(
            kwargs.get('side_effects', []), transition=self
        )
        self.callbacks = self.callbacks_class(
            kwargs.get('callbacks', []), transition=self
        )
        self.permissions = self.permissions_class(
            kwargs.get('permissions', []), transition=self
        )
        self.conditions = self.conditions_class(
            kwargs.get('conditions', []), transition=self
        )
        self.next_transition = self.next_transition_class(
            kwargs.get('next_transition')
        )

    def __str__(self):
        return f"Transition: {self.action_name} to {self.target}"

    def __repr__(self):
        return self.__str__()

    def is_valid(self, instance, user=None) -> bool:
        return (
            self.permissions.execute(instance, user)
            and self.conditions.execute(instance)
        )

    def change_state(self, state: State, **kwargs) -> UUID | None:
        process_class = kwargs.get('process_class', '')
        process_class_name = process_class.split('.')[-1] if process_class else ''
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.START.value} '
            f'{process_class_name} {self.action_name} {state.instance_key} '
            f'{kwargs.get("root_id")} {kwargs.get("parent_id")}',
            extra={'kwargs': redact_log_kwargs(kwargs), 'state_hash': state._get_hash()},
        )

        # lock() is atomic (cache.add / Redis SET NX) and returns False if
        # the state is already locked, so the acquire alone is sufficient.
        # A separate is_locked() pre-check only adds a TOCTOU window and a
        # redundant round-trip (a stale is_locked()==True could even reject
        # a transition the atomic lock() would have granted).
        #
        # No-arg call when no per-transition override is configured, so
        # custom State subclasses written against the pre-lock_timeout
        # ``lock(self)`` signature keep working (#142).
        locked = (
            state.lock()
            if self.lock_timeout is None
            else state.lock(self.lock_timeout)
        )
        if not locked:
            raise TransitionNotAllowed("State is locked")

        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.LOCK.value}'
        )

        # Revalidate under the lock. The source/condition checks in
        # get_transition_by_action_name ran before the lock was acquired;
        # by now a concurrent transition may have won the race and moved
        # the state (validate-then-lock TOCTOU). One cheap query closes it.
        # Any failure between acquisition and the side-effect machinery —
        # including the in_progress_state write itself (connection drop,
        # statement timeout, broken outer atomic) — must release the lock
        # or the instance's FSM freezes until the lock TTL expires.
        try:
            self._ensure_db_state_in_sources(state)
            self._ensure_no_background_in_flight(state)
            if self.in_progress_state:
                state.set_state(self.in_progress_state)
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
                    f'{self.in_progress_state}'
                )
        except Exception:
            state.unlock()
            raise

        self._init_transition_context(kwargs)
        self.side_effects.execute(state, **kwargs)
        return kwargs.get('tr_id')

    def complete_transition(self, state: State, **kwargs):
        """Write target state, unlock, then run callbacks.

        The lock is released **before** callbacks run, so a callback can
        safely trigger another transition on the same instance. If the
        worker crashes during callbacks they are lost — callbacks are
        best-effort.

        A failed target write must still release the lock (otherwise the
        instance's FSM freezes until the lock TTL): the transition fails
        loudly either way, but a leaked lock turns one failed request into
        hours of rejected transitions.
        """
        try:
            state.set_state(self.target)
        except Exception:
            transition_logger.error(
                f'{kwargs.get("tr_id")} target-state write failed for '
                f'{state.instance_key}; releasing the lock before re-raising.'
            )
            state.unlock()
            raise
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
            f'{self.target}'
        )

        self._release_lock(state, **kwargs)

        self.callbacks.execute(state, **kwargs)
        self.next_transition.execute(state, **kwargs)

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        # try/finally: a failed failed_state write (or a malformed
        # failure_side_effects bundle) must still release the lock; the
        # original side-effect exception keeps propagating out of
        # SideEffects.execute either way.
        try:
            if self.failed_state:
                state.set_state(self.failed_state)
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
                    f'{self.failed_state}'
                )

            self.failure_side_effects.execute(state, exception=exception, **kwargs)
        finally:
            self._release_lock(state, **kwargs)

        self.failure_callbacks.execute(state, exception=exception, **kwargs)

    @staticmethod
    def _release_lock(state: State, **kwargs):
        """Release the state lock — now, or at commit (#141).

        Inside an outer ``transaction.atomic()`` the target/failed state
        write is invisible to other connections until the block commits,
        while the cache lock is not transactional. Releasing immediately
        (the historical default) opens a window in which a second
        transition can acquire the lock, read the OLD committed state and
        run conflicting side-effects — the final state then depends on
        commit ordering.

        With ``DJANGO_LOGIC['DEFER_UNLOCK_UNTIL_COMMIT'] = True`` the
        unlock is deferred to ``transaction.on_commit`` so mutual
        exclusion covers the whole invisible span. Documented trade-offs
        (see README):

        * on rollback the hook never fires — the lock expires via its
          TTL, a *bounded* lockout (same failure mode as a crashed
          process). Rollback-prone flows should pair this with a
          per-transition ``lock_timeout``;
        * same-instance follow-ups (callbacks / ``next_transition``)
          inside the atomic block find the state still locked and are
          skipped (they are best-effort by contract) — chain them from
          ``transaction.on_commit`` in the caller instead.

        Only the paths that follow a successful state write defer; the
        early revalidation-failure unlock in ``change_state`` stays
        immediate (nothing was written, so there is no visibility window
        to protect and nothing to leak on rollback).
        """
        if _defer_unlock_until_commit():
            using = state.instance._state.db or DEFAULT_DB_ALIAS
            if transaction.get_connection(using).in_atomic_block:
                transaction.on_commit(state.unlock, using=using)
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.UNLOCK.value} '
                    f'deferred until commit'
                )
                return
        state.unlock()
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.UNLOCK.value}'
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
        """Reject a state-changing transition while a background transition
        is in flight on the same instance + process.

        The uncompleted ``TransitionMessage`` row is the durable in-flight
        marker for background work; the cache lock only guards short
        critical sections. Without this gate a synchronous transition could
        interleave with phase 2 and the two would overwrite each other's
        state writes. Checked under the lock, like the source revalidation.
        """
        from django.apps import apps

        if not apps.is_installed('django_logic.background'):
            return
        from django_logic.background.models import TransitionMessage

        in_flight = TransitionMessage.objects.filter(
            app_label=state.instance._meta.app_label,
            model_name=state.instance._meta.model_name,
            instance_id=str(state.instance.pk),
            process_name=state.process_name,
            is_completed=False,
        ).exists()
        if in_flight:
            raise TransitionNotAllowed(
                f"Transition '{self.action_name}' is not allowed: a "
                f"background transition is in progress for "
                f"{state.instance_key} (uncompleted TransitionMessage)."
            )


class Action(Transition):
    """Transition that does not change state on success.

    Still runs side-effects and callbacks. ``failed_state`` (if set)
    is applied on failure — but only when the state is not locked by an
    in-flight transition (see ``fail_transition``).

    Deliberate asymmetries vs :class:`Transition` — an Action does not
    change state, so it skips the state-change machinery entirely:

    * no cache lock, no under-the-lock source revalidation, and no
      background-in-flight gate (Actions may run while a background
      transition is in flight);
    * ``next_transition`` is NOT executed on success (note the divergence:
      a *BackgroundAction*'s phase 2 does run ``next_transition``);
    * ``in_progress_state`` is accepted but never written (it is only
      added to ``sources``); ``BackgroundAction`` rejects it outright.
    """

    def __init__(self, action_name: str, sources: list, **kwargs):
        super().__init__(action_name=action_name, sources=sources, target='', **kwargs)

    def __str__(self):
        return f"Action: {self.action_name}"

    def change_state(self, state: State, **kwargs) -> UUID | None:
        self._init_transition_context(kwargs)
        self.side_effects.execute(state, **kwargs)
        return kwargs.get('tr_id')

    def complete_transition(self, state: State, **kwargs):
        self.callbacks.execute(state, **kwargs)

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        """Run the failure path WITHOUT unlocking.

        An Action never acquires the state lock (``change_state`` skips
        ``state.lock()``), so it must never release one either. Inheriting
        ``Transition.fail_transition`` would call ``state.unlock()`` and,
        because the lock key is derived only from instance+field, delete
        the lock a concurrent ``Transition`` on the same instance/field
        legitimately holds — and, for ``RedisState``, discard the cached
        in-progress state stored under that same key. This mirrors the
        lock/unlock asymmetry already present in ``complete_transition``.

        ``failed_state`` is only written when the state is NOT currently
        locked: an Action holds no lock, so writing the state field while
        another transition is legitimately mid-flight would silently
        overwrite that transition's state ("last write wins"). When the
        write is skipped, the failure is still fully visible — the
        exception propagates and the failure hooks run.
        """
        if self.failed_state:
            if state.is_locked():
                transition_logger.error(
                    f'{kwargs.get("tr_id")} Action {self.action_name!r}: '
                    f'skipping failed_state={self.failed_state!r} write — '
                    f'{state.instance_key} is locked by an in-flight '
                    f'transition and an Action holds no lock.'
                )
            else:
                state.set_state(self.failed_state)
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
                    f'{self.failed_state}'
                )
        self.failure_side_effects.execute(state, exception=exception, **kwargs)
        self.failure_callbacks.execute(state, exception=exception, **kwargs)
