"""Transition — a single state-machine edge.

A ``Transition`` moves an instance from one of its source states to its
target state, running side-effects on success and either callbacks or
failure callbacks on completion. Everything happens synchronously, in
the caller's call frame — validate, lock, run, write the target state.

``Action`` is a transition that does not change state on success but
still runs side-effects and can set a ``failed_state`` on failure.

For background-executed transitions, see
``django_logic.background.BackgroundTransition``. That path has two
halves: enqueue (write ``in_progress_state`` + a ``TransitionMessage``
row in one transaction, then send the Celery task) and execute (the
worker runs the side-effects and writes the final state). Definitions
live in ``django_logic.background.transitions`` (enqueue) and
``django_logic.background.runner`` (execute).
"""
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, transaction

from django_logic.commands import (
    Callbacks,
    Conditions,
    NextTransition,
    Permissions,
    SideEffects,
    _run_in_savepoint,
    note_deferred_unlock,
)
from django_logic.exceptions import (
    TransitionNotAllowed,
    TransitionTemporarilyUnavailable,
)
from django_logic.logger import (
    redact_log_kwargs,
    transition_logger,
    TransitionEventType,
)
from django_logic.conf import defer_unlock_until_commit as _defer_unlock_until_commit
from django_logic.state import State


#: Names of the engine's OWN method parameters on the state-change path.
#: A caller kwarg carrying one raises TypeError inside ``fail_transition``/
#: ``_release_lock`` — on the failure path, after the lock was taken: no
#: ``failed_state``, the real exception replaced, the lock leaked until TTL.
#: Refused up front, before anything is acquired. Distinct from
#: ``process._RESERVED_KWARGS`` (names the engine forwards itself —
#: documented, not refused).
_ENGINE_PARAM_KWARGS = frozenset({'state', 'exception', 'deferrable'})


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

    side_effects_class = SideEffects
    callbacks_class = Callbacks
    failure_callbacks_class = Callbacks
    permissions_class = Permissions
    conditions_class = Conditions

    #: ``True`` on ``BackgroundTransition``. A public, stable attribute
    #: rather than an ``isinstance`` check: ``Process.__init_subclass__``
    #: reads it to enforce unique background action names without importing
    #: ``BackgroundTransition`` (which would close the cycle
    #: ``process → background.transitions → transition → process``), and
    #: consumers introspect it the same way.
    is_background: bool = False

    def __init__(self, action_name: str, sources: list, target: str, **kwargs):
        self.action_name = action_name
        self.target = target
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
            # Background-only (0.12.0). On a background transition the
            # state is written atomically with the TransitionMessage row,
            # so every marked instance names its exact transition. A
            # synchronous transition wrote it under a cache lock with no
            # durable record: a hard-killed worker left the instance
            # parked in a state with no outbound edges and nothing that
            # could ever move it. Without that write a killed sync run
            # rolls back to its source state and can simply be run again.
            # Model a visible "busy" step as a real state instead: a
            # fast transition into it, chained via next_transition to the
            # transition that does the work (see the README migration
            # note).
            raise ImproperlyConfigured(
                f"Transition {action_name!r}: in_progress_state is only "
                f"supported on BackgroundTransition, where it is written "
                f"atomically with the durable TransitionMessage row. On a "
                f"synchronous transition that write is a record-less dead "
                f"end. Model the busy step as a real state with an "
                f"explicit transition into it, or make this transition a "
                f"BackgroundTransition."
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
        # Built through class attributes like the other four, so all five
        # bundles are swappable.
        self.failure_callbacks = self.failure_callbacks_class(
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
        self.conditions = self.conditions_class(
            kwargs.get('conditions', [])
        )
        self.next_transition = NextTransition(kwargs.get('next_transition'))

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
        # Before the lock: a clash here must not become a leaked lock.
        _refuse_engine_param_kwargs(self.action_name, kwargs)
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
        # freezes until the lock TTL expires. (No state is written under the
        # lock before the side-effects anymore: in_progress_state is
        # background-only since 0.12.0 — a sync run that dies leaves the
        # instance at its source state, ready to run again, with nothing
        # to sweep.)
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
        """Write target state, release the lock, then run callbacks.

        By default the lock is released **before** callbacks run, so a
        callback can safely trigger another transition on the same
        instance. Under ``DEFER_UNLOCK_UNTIL_COMMIT`` inside an open
        transaction the release rides ``transaction.on_commit`` instead —
        callbacks then still find the state locked, and a same-instance
        follow-up is skipped as best-effort (see ``_release_lock``). If
        the worker crashes during callbacks they are lost — callbacks are
        best-effort.

        A failed target write must still release the lock (otherwise the
        instance's FSM freezes until the lock TTL): the transition fails
        loudly either way, but a leaked lock turns one failed request into
        hours of rejected transitions. The release follows the same
        deferral rule as ``fail_transition`` — immediate, since the rejected
        write means nothing landed under this lock.
        """
        try:
            state.set_state(self.target)
        except Exception:
            transition_logger.error(
                f'{kwargs.get("tr_id")} target-state write failed for '
                f'{state.instance_key}; releasing the lock before re-raising.'
            )
            # Same deferral rule as fail_transition: nothing was written
            # under this lock (the rejected target never landed, and sync
            # transitions write no marker since 0.12.0), so there is no
            # invisible span to protect — release now; deferring would only
            # leak the lock until TTL when the outer transaction rolls back.
            self._release_lock(state, deferrable=False, **kwargs)
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
        #
        # Deferral only applies when a state write actually happened
        # under this lock — the failed_state written below (sync
        # transitions write no in-progress state since 0.12.0). A failure
        # that wrote nothing has no invisible span to protect, so the unlock
        # stays immediate (deferring would only leak the lock until TTL when
        # the outer transaction rolls back).
        wrote_state = False
        try:
            if self.failed_state:
                # Savepointed so a rejected failed_state write cannot replace
                # the original side-effect exception on its way out. The
                # docstring above promised "the original exception keeps
                # propagating either way"; without this the write's own
                # exception won and the real cause was lost.
                previous = state.get_state()
                try:
                    # The instance's alias, not DEFAULT: set_state routes its
                    # write with hints={'instance': ...}, so a savepoint opened
                    # on DEFAULT would guard the wrong connection.
                    # require_commit: the else-branch logs SET_STATE, so a
                    # silently discarded savepoint must take the honest
                    # except-path instead.
                    _run_in_savepoint(
                        state.instance._state.db or DEFAULT_DB_ALIAS,
                        lambda: state.set_state(self.failed_state),
                        require_commit=True,
                    )
                except Exception as write_error:
                    # A silent rollback discards the write AFTER set_state
                    # refreshed the attribute to the savepoint's uncommitted
                    # value — restore it, or the failure hooks and the sync
                    # caller observe a state the database never had.
                    setattr(state.instance, state.field_name, previous)
                    transition_logger.error(
                        f'{kwargs.get("tr_id")} could not write failed_state '
                        f'{self.failed_state!r} on {state.instance_key}: '
                        f'{type(write_error).__name__}: {write_error}. The '
                        f'original failure is re-raised unchanged.',
                        exc_info=True,
                    )
                else:
                    wrote_state = True
                    # Inside the else: a rejected write must NOT log
                    # SET_STATE. The line is the state-change record the
                    # trace and log-based assertions read, so emitting it
                    # for a write that did not land would be a false entry.
                    transition_logger.info(
                        f'{kwargs.get("tr_id")} '
                        f'{TransitionEventType.SET_STATE.value} '
                        f'{self.failed_state}'
                    )
        finally:
            self._release_lock(state, deferrable=wrote_state, **kwargs)

        self.failure_callbacks.execute(state, exception=exception, **kwargs)

    @staticmethod
    def _release_lock(state: State, deferrable: bool = True, **kwargs):
        """Release the state lock — now, or at commit under
        ``DJANGO_LOGIC['DEFER_UNLOCK_UNTIL_COMMIT']``.

        Deferring extends mutual exclusion over the span where a state
        write is committed-but-invisible to other connections. The
        trade-offs, and when to enable it, are in the README.

        ``deferrable`` is False on the paths where nothing was written
        under the lock (the early revalidation-failure unlock, a rejected
        target write, and a failure path with no ``failed_state`` landed):
        with no visibility window to protect, deferring would only leak the
        lock until TTL on rollback.
        """
        if deferrable and _defer_unlock_until_commit():
            using = state.instance._state.db or DEFAULT_DB_ALIAS
            if transaction.get_connection(using).in_atomic_block:
                transaction.on_commit(state.unlock, using=using)
                # Registered so a hook savepoint that rolls back can
                # release this lock instead of silently discarding the
                # on_commit hook with it (commands._run_in_savepoint).
                note_deferred_unlock(using, state)
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.UNLOCK.value} '
                    f'{state.instance_key} deferred until commit'
                )
                return
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

    @staticmethod
    def _background_in_flight(state: State) -> bool:
        """Whether an uncompleted ``TransitionMessage`` exists for this
        instance + process (the cache lock only guards short critical
        sections). Only meaningful while holding the lock: enqueue needs
        the lock to create a new row, so the answer cannot flip
        underneath the holder.

        Bare existence, deliberately stricter than the public
        ``in_flight()`` probe: the Action's write-skip must never clobber
        an uncompleted row's instance, stranded or not — force-failing a
        stranded row's instance is what the retired sweep used to do.
        """
        from django.apps import apps

        if not apps.is_installed('django_logic.background'):
            return False
        from django_logic.background.models import TransitionMessage

        return TransitionMessage.in_flight_for(
            state.instance, state.process_name,
        ).exists()

    def _ensure_no_background_in_flight(self, state: State) -> None:
        """Reject a state-changing transition while a background transition
        is in progress on the same instance + process.

        Without this gate a synchronous transition could interleave with
        the worker and the two would overwrite each other's state writes.
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

        if not apps.is_installed('django_logic.background'):
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


class Action(Transition):
    """Transition that does not change state on success.

    Still runs side-effects and callbacks. ``failed_state`` (if set)
    is applied on failure — but only when the state is not locked by an
    in-progress transition (see ``fail_transition``).

    Deliberate asymmetries vs :class:`Transition` — an Action does not
    change state, so it skips the state-change machinery entirely:

    * no cache lock around the side-effects, no under-the-lock source
      revalidation, and no background-in-progress gate (Actions may run
      while a background transition is in progress); the one lock an
      Action takes is short-lived, scoped to the ``failed_state`` write
      in ``fail_transition`` — and that write is skipped while an
      uncompleted ``TransitionMessage`` exists, because the worker owns
      the state field until the row completes;
    * ``next_transition`` is NOT executed on success (note the
      divergence: a *BackgroundAction*'s worker path does run
      ``next_transition``);
    * ``in_progress_state`` is rejected like any synchronous transition's
      (background-only since 0.12.0); ``BackgroundAction`` rejects it too.
    """

    def __init__(self, action_name: str, sources: list, **kwargs):
        super().__init__(action_name=action_name, sources=sources, target='', **kwargs)

    def __str__(self):
        return f"Action: {self.action_name}"

    def change_state(self, state: State, **kwargs) -> UUID | None:
        # An Action takes no lock on the success path, so there is none to
        # leak — but the failure path still loses failed_state and the
        # original exception.
        _refuse_engine_param_kwargs(self.action_name, kwargs)
        self._init_transition_context(kwargs)
        self.side_effects.execute(state, **kwargs)
        return kwargs.get('tr_id')

    def complete_transition(self, state: State, **kwargs):
        self.callbacks.execute(state, **kwargs)

    def fail_transition(self, state: State, exception: Exception, **kwargs):
        """Run the failure path, taking the lock only around the write.

        An Action runs its side-effects without the state lock, so
        inheriting ``Transition.fail_transition`` — whose unconditional
        ``state.unlock()`` would delete the lock a concurrent ``Transition``
        on the same instance/field legitimately holds — is not an option.
        This mirrors the lock/unlock asymmetry already present in
        ``complete_transition``.

        ``failed_state`` is written only under an atomically-acquired lock:
        checking ``is_locked()`` and then writing left a window for a
        concurrent transition to start between the check and the write, and
        the Action's stale write then clobbered that transition's state.
        The cache lock only covers sync runs and enqueue, so under the
        lock the uncompleted ``TransitionMessage`` is consulted too:
        while one exists, the worker owns the state field and the write
        is skipped — otherwise it would supersede the worker (or be
        destroyed by its target write). Whenever the write is skipped,
        the failure is still fully visible — the exception propagates
        and the failure hooks run.
        """
        if self.failed_state:
            if not state.lock():
                transition_logger.error(
                    f'{kwargs.get("tr_id")} Action {self.action_name!r}: '
                    f'skipping failed_state={self.failed_state!r} write — '
                    f'{state.instance_key} is locked by an in-progress '
                    f'transition and an Action must not overwrite its state.'
                )
            else:
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.LOCK.value} '
                    f'{state.instance_key}'
                )
                wrote_state = False
                try:
                    # Probe guarded: the side-effect that brought us here
                    # may have rollback-poisoned the connection (or the
                    # database may be down), in which case the probe itself
                    # raises. Escaping here replaced the original exception
                    # with the probe's and skipped both failure hook bundles
                    # — breaking this method's own contract. Unknown means
                    # do not write.
                    try:
                        in_flight = self._background_in_flight(state)
                    except Exception as probe_error:
                        in_flight = None
                        transition_logger.error(
                            f'{kwargs.get("tr_id")} Action '
                            f'{self.action_name!r}: could not probe for an '
                            f'in-progress background transition on '
                            f'{state.instance_key}: '
                            f'{type(probe_error).__name__}: {probe_error}. '
                            f'Skipping the failed_state write; the original '
                            f'failure is re-raised unchanged.',
                            exc_info=True,
                        )
                    if in_flight:
                        transition_logger.error(
                            f'{kwargs.get("tr_id")} Action '
                            f'{self.action_name!r}: skipping failed_state='
                            f'{self.failed_state!r} write — an uncompleted '
                            f'TransitionMessage owns the state field of '
                            f'{state.instance_key} until the worker '
                            f'completes it.'
                        )
                    elif in_flight is not None:
                        # Savepointed like Transition.fail_transition: a
                        # rejected write must not replace the original
                        # side-effect exception on its way out, and must not
                        # log a SET_STATE line for a write that never landed.
                        previous = state.get_state()
                        try:
                            # The instance's alias, not DEFAULT: set_state
                            # routes its write with hints={'instance': ...},
                            # so a savepoint opened on DEFAULT would guard
                            # the wrong connection. require_commit: a
                            # silently discarded savepoint must take the
                            # honest except-path.
                            _run_in_savepoint(
                                state.instance._state.db or DEFAULT_DB_ALIAS,
                                lambda: state.set_state(self.failed_state),
                                require_commit=True,
                            )
                        except Exception as write_error:
                            # Restore the attribute a discarded savepoint
                            # left refreshed — hooks must not see a state
                            # the database never had.
                            setattr(
                                state.instance, state.field_name, previous)
                            transition_logger.error(
                                f'{kwargs.get("tr_id")} Action '
                                f'{self.action_name!r}: could not write '
                                f'failed_state {self.failed_state!r} on '
                                f'{state.instance_key}: '
                                f'{type(write_error).__name__}: '
                                f'{write_error}. The original failure is '
                                f're-raised unchanged.',
                                exc_info=True,
                            )
                        else:
                            wrote_state = True
                            transition_logger.info(
                                f'{kwargs.get("tr_id")} '
                                f'{TransitionEventType.SET_STATE.value} '
                                f'{self.failed_state}'
                            )
                finally:
                    # The shared release path: emits the Unlock lifecycle
                    # line and honours DEFER_UNLOCK_UNTIL_COMMIT when a
                    # state write landed under the lock.
                    self._release_lock(state, deferrable=wrote_state, **kwargs)
        self.failure_callbacks.execute(state, exception=exception, **kwargs)
