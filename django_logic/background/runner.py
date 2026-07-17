"""Phase 2 execution.

``run_background_transition(tm_id)`` owns a single attempt at executing
a durable background transition. It runs the same way in:

* the Celery task wrapper (:mod:`django_logic.background.tasks`), and
* sync mode, directly after phase 1 in the same process.

Structure:

1. One ``atomic`` block that:

   * locks the TransitionMessage row with ``select_for_update(nowait=True)``
     (another worker already holds it → raise ``OperationalError`` →
     caller exits silently),
   * restores the instance + transition,
   * verifies the instance is still in the state phase 1 left behind
     (the *state guard* — on mismatch the row completes as superseded
     and side-effects are skipped, so a manual ops fix is never
     overwritten),
   * runs each side-effect in order **inside a savepoint** — a failed
     attempt rolls back every side-effect write (and keeps the outer
     transaction healthy even when the side-effect raised a genuine
     ``DatabaseError``, so the error bookkeeping below always works),
   * on success, writes ``target`` state (for ``BackgroundTransition``)
     and marks the TM completed,
   * on failure, records the error and either leaves the TM for retry
     or, at ``MAX_ERRORS``, writes ``failed_state`` and marks completed.

2. After the atomic block (best-effort):

   * success callbacks + ``next_transition`` (success path), or
   * failure callbacks (terminal-failure path).

Side-effect exceptions re-raise out of ``run_background_transition``
only in **sync mode**, so inline callers and tests can ``assertRaises``
directly. In **Celery mode** they are swallowed after being fully
recorded on the row (``errors_count`` + ``last_error``, or terminal
``failed_state`` + completion) — the periodic starter owns retries, and
re-raising out of an ``acks_late`` task would spam task-failure alerts
and risk broker redelivery on top of the periodic retry.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.db import OperationalError, transaction

from django_logic.background import settings as bg_settings
from django_logic.background.models import TransitionMessage
from django_logic.background.observability import set_sentry_context
from django_logic.background.serializers import deserialize_kwargs
from django_logic.background.transitions import BackgroundAction, BackgroundTransition
from django_logic.logger import TransitionEventType, transition_logger
from django_logic.process import _transition_context


@dataclass
class _Outcome:
    """What phase 2's atomic block produced — drives best-effort phase 3."""

    terminal: bool  # Work is done (target, failed, or nothing to run)
    succeeded: bool
    exception: BaseException | None = None
    transition: BackgroundTransition | None = None
    state_obj: Any = None
    kwargs: dict | None = None


def run_background_transition(transition_message_id: int) -> None:
    """Run a single attempt at the transition identified by ``transition_message_id``.

    Designed to be call-compatible from both a Celery task and an
    inline sync dispatcher.
    """
    try:
        outcome = _run_atomic(transition_message_id)
    except _StopRetry as exc:
        # The atomic block rolled back, so we couldn't mark_as_completed
        # from inside it. Do it here, in its own statement, to stop the
        # retry loop from picking the row up forever.
        _mark_unrestorable_completed(exc.tm_id)
        return
    except _NothingToDo:
        return

    # Phase 3 (best-effort).
    if outcome.terminal and outcome.succeeded and outcome.transition is not None:
        _run_success_hooks(outcome)
    elif outcome.terminal and not outcome.succeeded and outcome.transition is not None:
        _run_failure_hooks(outcome)

    if outcome.exception is not None:
        # Sync mode propagates the exception so the inline caller / tests
        # can react (assertRaises, surface the failure to the request).
        # Celery mode must NOT re-raise: the outcome is already fully
        # recorded on the row (errors_count + last_error for a retryable
        # failure; failed_state + is_completed for a terminal one), the
        # periodic starter owns retries, and re-raising out of an
        # acks_late task would both spam task-failure alerts for an
        # already-resolved row and risk broker redelivery on top of the
        # periodic retry.
        from django_logic.background.dispatch import _current_mode
        if _current_mode() == bg_settings.EXECUTION_SYNC:
            raise outcome.exception


class _NothingToDo(Exception):
    """Internal signal: the TM is already completed, missing, or locked
    by another worker. Caller should exit silently."""


class _StopRetry(Exception):
    """Internal signal: the TM refers to a model/transition that no
    longer exists. The atomic block rolled back; the outer handler
    marks the TM completed in its own statement so retries stop."""

    def __init__(self, tm_id: int):
        self.tm_id = tm_id


def _mark_unrestorable_completed(tm_id: int) -> None:
    """Mark an unrestorable TM completed so the periodic starter stops
    re-dispatching it forever.

    Runs as a single UPDATE outside the (already-exited, rolled-back)
    phase-2 atomic block. Durability depends on the execution mode:

    * Celery mode — phase 2 runs as the top-level unit of work with no
      surrounding transaction, so this UPDATE autocommits and is durable.
      This is the path the original infinite-retry bug lived on.
    * Sync mode — phase 1 (which created the row) and phase 2 run in the
      same call stack and share the caller's transaction state. If the
      caller wraps the whole call in ``atomic()`` and later rolls back,
      this UPDATE rolls back too — but so does the phase-1 INSERT, so there
      is no surviving row to re-dispatch and the stop-retry guarantee still
      holds. It is NOT a write that survives an *independent* parent
      rollback on its own; correcting an earlier docstring that claimed so.
    """
    from django.utils import timezone

    try:
        TransitionMessage.objects.filter(pk=tm_id, is_completed=False).update(
            is_completed=True,
            completed_at=timezone.now(),
        )
    except Exception as e:
        transition_logger.error(
            f'Failed to mark unrestorable TransitionMessage#{tm_id} '
            f'completed: {e}'
        )


def abandon_timed_out_attempt(tm_id: int) -> bool:
    """Record a synthetic timeout error on a TM whose current attempt
    has exceeded its declared ``timeout_seconds``.

    Skips rows currently held by a worker (``select_for_update(nowait)``
    → OperationalError) — we only act on abandoned attempts. When the
    error count reaches ``MAX_ERRORS`` the row is finalized in the same
    atomic block (failed_state + failure_side_effects + mark_as_completed)
    so the retry loop stops.

    .. note::

        The watchdog cannot distinguish a genuinely abandoned attempt
        (worker crashed / lost DB connection) from a live-but-slow one
        that has kept its Python state but dropped its row lock. In the
        latter case, the watchdog will acquire the row and re-dispatch
        while the original worker is still executing side-effects. This
        is safe per the reliability contract: side-effects MUST be
        idempotent (§2.7), so re-running them from scratch is acceptable.
        The original worker's eventual ``mark_as_completed`` / ``record_error``
        will either succeed (completing the row) or fail harmlessly
        against a completed row.

    Returns True if the row was touched, False if skipped.
    """
    hooks = None
    with transaction.atomic():
        try:
            tm = (
                TransitionMessage.objects
                .select_for_update(nowait=True)
                .get(pk=tm_id, is_completed=False)
            )
        except TransitionMessage.DoesNotExist:
            return False
        except OperationalError:
            transition_logger.info(
                f'watchdog: TransitionMessage#{tm_id} currently locked '
                f'by a worker; deferring abandon'
            )
            return False

        transition_logger.error(
            f'watchdog: TransitionMessage#{tm.pk} '
            f'{tm.app_label}.{tm.model_name}#{tm.instance_id} '
            f'{tm.transition_name} exceeded timeout_seconds='
            f'{tm.timeout_seconds}; recording timeout error'
        )
        err = TimeoutError(
            f'[watchdog timeout] attempt exceeded '
            f'timeout_seconds={tm.timeout_seconds}'
        )
        tm.record_error(err)

        max_errors = bg_settings.max_errors()
        if tm.errors_count >= max_errors:
            # Terminal. Finalize inside this same atomic — we already
            # hold the row lock so we cannot recurse through
            # finalize_stuck_attempt (deadlock).
            hooks = _finalize_terminal_from_watchdog(tm, err, source='watchdog')

    # Run failure_callbacks after the atomic commits and the row lock is
    # released (phase 3, best-effort) — see _run_failure_callbacks.
    if hooks is not None:
        _run_failure_callbacks(hooks)
    return True


def finalize_stuck_attempt(tm_id: int) -> bool:
    """Force a stuck (``errors_count >= MAX_ERRORS``, uncompleted) TM
    into a terminal state (``failed_state`` + ``failure_side_effects``
    + ``mark_as_completed``).

    Called by ``detect_stuck_transitions``. If the row is currently
    locked by a worker running phase 2 we exit silently — the running
    attempt will finalize on its own. Otherwise we restore the
    transition, run the terminal-failure sequence, and mark completed.

    Returns True if the row was finalized, False if skipped.
    """
    hooks = None
    with transaction.atomic():
        try:
            tm = (
                TransitionMessage.objects
                .select_for_update(nowait=True)
                .get(pk=tm_id, is_completed=False)
            )
        except TransitionMessage.DoesNotExist:
            return False
        except OperationalError:
            transition_logger.info(
                f'detect_stuck: TransitionMessage#{tm_id} locked by a '
                f'worker; deferring finalization'
            )
            return False

        transition_logger.error(
            f'Stuck transition: TransitionMessage#{tm.pk} '
            f'{tm.app_label}.{tm.model_name}#{tm.instance_id} '
            f'{tm.transition_name} queue={tm.queue_name} '
            f'errors={tm.errors_count} '
            f'last_error={tm.last_error_message!r}; forcing terminal state'
        )
        # Rehydrate an exception from the stored last_error_message so
        # failure_side_effects see the same error shape the final in-task
        # attempt would have seen.
        err = RuntimeError(
            f'[detect_stuck] {tm.last_error_message or "transition stuck"}'
        )
        hooks = _finalize_terminal_from_watchdog(tm, err, source='detect_stuck')

    # Run failure_callbacks after the atomic commits (phase 3, best-effort).
    if hooks is not None:
        _run_failure_callbacks(hooks)
    return True


def _finalize_terminal_from_watchdog(
    tm: TransitionMessage,
    exception: BaseException,
    source: str,
):
    """Shared terminal-failure path for the watchdog / detect-stuck tasks.

    Must run inside the caller's atomic block, with the TM row already
    locked. Mirrors ``_handle_failure``'s terminal branch: set
    failed_state, run failure_side_effects (capturing swallowed errors
    onto the TM), mark completed.

    If the transition can't be restored (model uninstalled / transition
    renamed), we still mark_as_completed so the retry loop stops;
    failed_state and failure_side_effects are skipped — there's nothing
    to call them on.

    Returns the ``(transition, state, kwargs, exception)`` tuple the caller
    needs to run ``failure_callbacks`` *after* its atomic block commits
    (so callbacks don't run while holding the row lock, matching the
    in-task phase-3 timing), or ``None`` when the row was unrestorable
    (nothing to run callbacks on).
    """
    try:
        _, process, transition = _restore(tm)
    except _RestoreError:
        # No attempt ran here, so started_at (if any) belongs to an
        # abandoned attempt — don't record a misleading duration.
        tm.mark_as_completed(measure_duration=False)
        return None

    kwargs = deserialize_kwargs(tm.kwargs)
    # Mirror the sync path: side-effects/callbacks may read ``context``.
    kwargs.setdefault('context', {})
    state = process.state

    if transition.failed_state:
        # Same state guard as the phase-2 attempt path: a safety-net task
        # finalizing a long-stranded row must not clobber a state change
        # made in the meantime (manual ops fix, external write).
        matches, expected, current = _state_guard_matches(transition, state)
        if matches or (
            bg_settings.phase2_state_guard() == bg_settings.STATE_GUARD_WARN
        ):
            state.set_state(transition.failed_state)
            transition_logger.info(
                f'{source}: set failed_state={transition.failed_state} '
                f'on {state.instance_key}'
            )
        else:
            transition_logger.error(
                f'{source}: NOT writing failed_state='
                f'{transition.failed_state!r} on {state.instance_key} — '
                f'expected {expected}, found {current!r}; the external '
                f'state change wins.'
            )

    # Symmetric with _handle_failure: run failure_side_effects inside
    # the atomic block (own savepoint), capture any swallowed exception
    # on the TM.
    fse_error = _run_failure_side_effects_isolated(
        transition, state, exception, kwargs
    )
    if fse_error is not None:
        tm.record_failure_side_effect_error(fse_error)
    # A safety-net finalization is not a worker attempt; started_at points
    # at the abandoned attempt, so don't let it inflate duration_ms.
    tm.mark_as_completed(measure_duration=False)
    return (transition, state, kwargs, exception)


def _run_failure_callbacks(hooks) -> None:
    """Run a terminal row's ``failure_callbacks`` best-effort, *after* the
    finalizing atomic block has committed and released the row lock.

    Mirrors ``_run_failure_hooks`` for rows finalized by the watchdog /
    detect_stuck tasks, so ``failure_callbacks`` fire on terminal failure
    regardless of whether the row hit MAX_ERRORS in-task or via a
    safety-net task. ``Callbacks.execute`` already swallows exceptions; the
    extra guard here is belt-and-suspenders against a malformed hook list.
    """
    transition, state, kwargs, exception = hooks
    try:
        transition.failure_callbacks.execute(
            state, exception=exception, **(kwargs or {})
        )
    except Exception as e:
        transition_logger.error(
            f'{(kwargs or {}).get("tr_id")} failure_callbacks failed '
            f'(best-effort, swallowed): {e}',
            exc_info=True,
        )


def _run_atomic(tm_id: int) -> _Outcome:
    # Invariant: everything that must survive together lives inside this
    # atomic block — row lock, mark_as_started, side-effects, and either
    # mark_as_completed (on success / terminal failure) or errors_count
    # increment (on retryable failure). Moving any of them out, in
    # particular the mark_as_* calls, is what broke the unrestorable-row
    # path (see _StopRetry). Don't do it.
    with transaction.atomic():
        try:
            tm = (
                TransitionMessage.objects
                .select_for_update(nowait=True)
                .get(pk=tm_id, is_completed=False)
            )
        except TransitionMessage.DoesNotExist as exc:
            transition_logger.info(
                f'TransitionMessage#{tm_id} already completed or missing; '
                f'nothing to do'
            )
            raise _NothingToDo() from exc
        except OperationalError as exc:
            transition_logger.info(
                f'TransitionMessage#{tm_id} locked by another worker; '
                f'skipping this attempt'
            )
            raise _NothingToDo() from exc

        # Per-transition monitoring identity (Sentry transaction name + tags);
        # best-effort, no-op without sentry-sdk. See observability.py / issue #78.
        set_sentry_context(tm)

        kwargs = deserialize_kwargs(tm.kwargs)
        # Mirror the synchronous path (Transition._init_transition_context):
        # side-effects/callbacks may read a framework-provided ``context``
        # dict. serialize_kwargs drops it at phase 1, so rebuild it here —
        # otherwise a side-effect declared as ``def fn(instance, context,
        # **kwargs)`` works synchronously but raises in background mode.
        kwargs.setdefault('context', {})

        try:
            instance, process, transition = _restore(tm)
        except _RestoreError as exc:
            transition_logger.error(
                f'TransitionMessage#{tm.pk} cannot be restored: {exc}. '
                f'Marking completed to stop retries.'
            )
            # Don't mark_as_completed() here — we're inside an atomic
            # block that will roll back when we exit. The outer handler
            # in run_background_transition() performs the mark in a
            # fresh statement so the stop-retry flag actually persists.
            raise _StopRetry(tm.pk) from exc

        state = process.state

        # State guard: phase 2 restores by name and deliberately bypasses
        # the source-state gate, so without this check it would overwrite
        # any state change made while the row was pending — including a
        # manual ops fix. With retries spanning RETRY_MINUTES × MAX_ERRORS
        # that collision is a realistic production event.
        matches, expected, current = _state_guard_matches(transition, state)
        if not matches:
            note = (
                f'[superseded] phase-2 state guard: expected {expected}, '
                f'found {current!r} — the instance was moved by something '
                f'else while this transition was pending. Side-effects '
                f'skipped; the external state change wins.'
            )
            if bg_settings.phase2_state_guard() == bg_settings.STATE_GUARD_ENFORCE:
                transition_logger.error(
                    f'{kwargs.get("tr_id")} TransitionMessage#{tm.pk} '
                    f'{transition.action_name} {state.instance_key}: {note}'
                )
                tm.mark_as_superseded(note)
                return _Outcome(terminal=True, succeeded=False)
            transition_logger.warning(
                f'{kwargs.get("tr_id")} TransitionMessage#{tm.pk} '
                f'{transition.action_name} {state.instance_key}: state '
                f'guard mismatch (expected {expected}, found {current!r}) '
                f"— PHASE2_STATE_GUARD='warn', running anyway."
            )

        # Record the start of this attempt. Overwritten on every retry so
        # the watchdog (uncompleted AND started_at < cutoff) tracks the
        # current attempt, not the first one.
        tm.mark_as_started()
        token = _transition_context.set(
            {
                'root_id': kwargs.get('root_id'),
                'tr_id': kwargs.get('tr_id'),
            }
        )
        try:
            transition_logger.info(
                f'{kwargs.get("tr_id")} Phase2 Start '
                f'{transition.action_name} {state.instance_key} '
                f'queue={tm.queue_name}'
            )
            try:
                # Savepoint: a failed attempt rolls back every side-effect
                # write (all-or-nothing per attempt), and a genuine
                # DatabaseError raised by a side-effect poisons only the
                # savepoint — the outer transaction stays healthy so
                # record_error / mark_as_completed below always work.
                # Without it, a DB error here made record_error itself
                # raise TransactionManagementError: the error was never
                # recorded, errors_count never reached MAX_ERRORS, and the
                # row was re-dispatched forever while blocking every
                # future background transition on the instance.
                with transaction.atomic():
                    for command in transition.side_effects.commands:
                        transition_logger.info(
                            f'{kwargs.get("tr_id")} '
                            f'{TransitionEventType.SIDE_EFFECT.value} '
                            f'{getattr(command, "__name__", repr(command))}'
                        )
                        command(instance, **kwargs)
            except Exception as error:
                return _handle_failure(tm, transition, state, kwargs, error)
            else:
                return _handle_success(tm, transition, state, kwargs)
        finally:
            _transition_context.reset(token)


def _handle_success(
    tm: TransitionMessage,
    transition: BackgroundTransition,
    state,
    kwargs: dict,
) -> _Outcome:
    if not isinstance(transition, BackgroundAction):
        state.set_state(transition.target)
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
            f'{transition.target}'
        )
    tm.mark_as_completed()
    transition_logger.info(
        f'{kwargs.get("tr_id")} {TransitionEventType.COMPLETE.value}'
    )
    return _Outcome(
        terminal=True,
        succeeded=True,
        transition=transition,
        state_obj=state,
        kwargs=kwargs,
    )


def _handle_failure(
    tm: TransitionMessage,
    transition: BackgroundTransition,
    state,
    kwargs: dict,
    error: BaseException,
) -> _Outcome:
    tm.record_error(error)
    transition_logger.error(
        f'{kwargs.get("tr_id")} {TransitionEventType.FAIL.value}: '
        f'{type(error).__name__}: {error}',
        exc_info=True,
    )

    max_errors = bg_settings.max_errors()
    if tm.errors_count < max_errors:
        # Leave uncompleted → periodic starter will retry.
        return _Outcome(
            terminal=False,
            succeeded=False,
            exception=error,
            transition=transition,
            state_obj=state,
            kwargs=kwargs,
        )

    # Terminal failure: write failed_state (if any) and mark completed.
    if transition.failed_state:
        state.set_state(transition.failed_state)
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
            f'{transition.failed_state}'
        )
    # failure_side_effects run inside the atomic block, in their own
    # savepoint — idempotent per the reliability contract. A broken
    # cleanup path rolls back its own writes and cannot poison the outer
    # transaction; the swallowed exception is surfaced on the TM
    # (otherwise cleanup bugs are invisible).
    fse_error = _run_failure_side_effects_isolated(
        transition, state, error, kwargs
    )
    if fse_error is not None:
        tm.record_failure_side_effect_error(fse_error)
    tm.mark_as_completed()
    return _Outcome(
        terminal=True,
        succeeded=False,
        exception=error,
        transition=transition,
        state_obj=state,
        kwargs=kwargs,
    )


def _run_success_hooks(outcome: _Outcome) -> None:
    assert outcome.transition is not None
    try:
        outcome.transition.callbacks.execute(
            outcome.state_obj, **(outcome.kwargs or {})
        )
    except Exception as e:
        transition_logger.error(
            f'{(outcome.kwargs or {}).get("tr_id")} callbacks failed '
            f'(best-effort, swallowed): {e}',
            exc_info=True,
        )
    try:
        outcome.transition.next_transition.execute(
            outcome.state_obj, **(outcome.kwargs or {})
        )
    except Exception as e:
        transition_logger.error(
            f'{(outcome.kwargs or {}).get("tr_id")} next_transition failed '
            f'(best-effort, swallowed): {e}',
            exc_info=True,
        )


def _run_failure_hooks(outcome: _Outcome) -> None:
    assert outcome.transition is not None
    try:
        outcome.transition.failure_callbacks.execute(
            outcome.state_obj,
            exception=outcome.exception,
            **(outcome.kwargs or {}),
        )
    except Exception as e:
        transition_logger.error(
            f'{(outcome.kwargs or {}).get("tr_id")} failure_callbacks failed '
            f'(best-effort, swallowed): {e}',
            exc_info=True,
        )


class _RestoreError(Exception):
    """The TransitionMessage refers to a model/instance/transition that
    no longer exists. The TM is marked completed to stop the retry loop.
    """


class _FailureSideEffectsRollback(Exception):
    """Internal: forces the failure_side_effects savepoint to roll back.

    ``FailureSideEffects.execute`` swallows the exception and *returns*
    it, so the savepoint context manager would otherwise commit the
    partial writes of a broken cleanup path — and, worse, a genuine
    ``DatabaseError`` raised inside a failure side-effect would leave
    the outer transaction poisoned. Raising this wrapper inside the
    savepoint rolls both problems back.
    """

    def __init__(self, error: BaseException):
        self.error = error


def _run_failure_side_effects_isolated(transition, state, exception, kwargs):
    """Run ``failure_side_effects`` inside a savepoint.

    Returns the swallowed exception (as ``FailureSideEffects.execute``
    does) or ``None``. On error, every write the failure side-effects
    made is rolled back and the outer transaction stays healthy, so the
    caller can still record the error and mark the row completed.
    """
    try:
        with transaction.atomic():
            error = transition.failure_side_effects.execute(
                state, exception=exception, **kwargs
            )
            if error is not None:
                raise _FailureSideEffectsRollback(error)
    except _FailureSideEffectsRollback as rollback:
        return rollback.error
    return None


def _state_guard_matches(transition, state) -> tuple[bool, str, str]:
    """Does the persisted state still match what phase 1 left behind?

    * Transition with ``in_progress_state`` — phase 1 wrote it, so the
      instance must still be exactly there.
    * Transition without ``in_progress_state`` / BackgroundAction — the
      instance must still be in one of the declared sources.

    Returns ``(matches, expected_description, current_state)``.
    """
    current = state.get_persisted_state()
    if transition.in_progress_state:
        return (
            current == transition.in_progress_state,
            f'in_progress_state {transition.in_progress_state!r}',
            current,
        )
    return (
        current in transition.sources,
        f'one of sources {transition.sources!r}',
        current,
    )


def _restore(tm: TransitionMessage):
    """Resolve ``(instance, process, transition)`` from a TM row."""
    try:
        app = apps.get_app_config(tm.app_label)
        model = app.get_model(tm.model_name)
    except LookupError as exc:
        raise _RestoreError(
            f'model {tm.app_label}.{tm.model_name} not installed'
        ) from exc

    try:
        # _base_manager, not objects: a filtered default manager (e.g. one
        # that hides archived/soft-deleted rows) would raise DoesNotExist
        # for an instance that still exists, and the restore-error path
        # would mark the message completed — stranding the instance in
        # in_progress_state with no failed_state and no retries. Framework
        # code reloading by pk must be immune to default-manager filtering
        # (Django's own convention for related-object loading).
        instance = model._base_manager.get(pk=tm.instance_id)
    except model.DoesNotExist as exc:
        raise _RestoreError(
            f'{tm.app_label}.{tm.model_name}#{tm.instance_id} not found'
        ) from exc

    recorded_path = (tm.kwargs or {}).get('process_class')
    try:
        process = getattr(instance, tm.process_name)
    except AttributeError:
        # Fall back to process_class stored in kwargs, if any.
        if not recorded_path:
            raise _RestoreError(
                f'instance has no process named {tm.process_name!r} and '
                f'no process_class stored on the message'
            )
        process = _load_process_from_path(instance, recorded_path, tm)
    else:
        # Verify the attribute resolved the same class phase 1 enqueued.
        # Every Process defaults to process_name='process', so a name
        # collision (directly-instantiated process vs the bound one, or a
        # rebind between deploy of phase 1 and phase 2) silently restores
        # the WRONG class — phase 2 would run side-effects the caller
        # never asked for. Prefer the recorded class on mismatch.
        if recorded_path:
            resolved_path = f'{type(process).__module__}.{type(process).__name__}'
            if resolved_path != recorded_path:
                transition_logger.warning(
                    f'TransitionMessage#{tm.pk}: process_name '
                    f'{tm.process_name!r} resolved to {resolved_path}, but '
                    f'the message was enqueued by {recorded_path}; using '
                    f'the recorded class.'
                )
                try:
                    process = _load_process_from_path(
                        instance, recorded_path, tm
                    )
                except Exception as exc:
                    transition_logger.error(
                        f'TransitionMessage#{tm.pk}: recorded process_class '
                        f'{recorded_path!r} could not be loaded ({exc}); '
                        f'falling back to the bound process '
                        f'{resolved_path}.'
                    )

    transition = _find_transition(process, tm)
    if transition is None:
        raise _RestoreError(
            f'transition {tm.transition_name!r} not found on process '
            f'{type(process).__module__}.{type(process).__name__}'
        )
    return instance, process, transition


def _load_process_from_path(instance, dotted: str, tm: TransitionMessage):
    module_path, class_name = dotted.rsplit('.', 1)
    module = importlib.import_module(module_path)
    process_class = getattr(module, class_name)
    # Phase 1 records the bound field on the message (0.4+). Rows created
    # before that fall back to inferring it from the bound process.
    field_name = tm.field_name or _infer_field_name(instance, tm)
    return process_class(field_name=field_name, instance=instance)


def _infer_field_name(instance, tm: TransitionMessage) -> str:
    # Legacy best effort for pre-0.4 rows (no recorded field_name): if the
    # model exposes a property with process_name, pull the field name from
    # its State. Otherwise default to 'state'.
    try:
        process = getattr(instance, tm.process_name)
        return process.field_name
    except Exception:
        return 'state'


def _find_transition(process, tm: TransitionMessage):
    """Resolve the exact background transition a ``TransitionMessage`` refers to.

    Phase 1 can enqueue a background transition declared on a *nested* process
    (the sync lookup ``get_transition_by_action_name`` recurses into
    ``nested_processes``), but the message records only the *bound*
    ``process_name``, so phase 2 restores the parent and must descend the
    ``nested_processes`` tree — each sub-process constructed with the parent's
    shared ``state``, exactly the way ``Process.get_available_transitions``
    does. Without this descent the nested transition is never found: the
    message is marked completed, the side-effects never run, and the instance
    is stranded in ``in_progress_state``.

    Phase 1 also records the (possibly nested) process class that DECLARES the
    transition on ``tm.owning_process_class``. When present it pins the search
    to that exact class, so an ``action_name`` shared across
    condition-disambiguated nested processes resolves to the one phase 1
    actually chose (see ``_validate_unique_background_action_names``). It is
    recorded for *every* background transition started through the Process
    entrypoint — for a transition on the bound process itself it equals the
    bound class. It is blank only for rows enqueued before this discriminator
    existed (pre-0.4.x) or, rarely, outside the Process entrypoint.

    When the owner is blank or no longer in the tree, we fall back to matching by
    ``action_name`` — but ONLY when the name identifies exactly one background
    transition across the whole tree. The relaxed validator now allows the same
    background ``action_name`` on distinct nested processes, so a fallback for an
    *ambiguous* name would be a coin flip between condition-disambiguated
    siblings: it could run the WRONG integration's side-effects (a
    ``BackgroundAction``, whose state guard cannot tell siblings apart) or strand
    the instance (a ``BackgroundTransition``, where the distinct
    ``in_progress_state`` makes the state guard supersede the row). So when an
    owner-less row's name is ambiguous we refuse to guess and raise
    ``_RestoreError`` — the row is finalized (retries stop) without running any
    side-effects, which is the safe, contained outcome. (This only arises for a
    row in flight across the exact deploy that turns a unique background
    ``action_name`` into a shared nested one; drain such rows before that
    refactor — see the upgrade note in the changelog.)

    Only ``is_background`` transitions are candidates: phase 2 never restores a
    synchronous transition (a ``TransitionMessage`` is created solely by a
    background transition's phase 1). A state-aware lookup would not work here
    either — phase 2 runs while the instance sits in ``in_progress_state`` (not
    in the transition's declared ``sources``), and the sync path's lookup is
    gated on state membership; we bypass that gate deliberately.
    """
    owning_path = (tm.owning_process_class or '').strip()
    if owning_path:
        found = _find_background_transition_in_owner(
            process, tm.transition_name, owning_path
        )
        if found is not None:
            return found
        # The owner was recorded but is not in the tree — e.g. the nested
        # process class was renamed/removed between the phase-1 and phase-2
        # deploys. Fall through to the name-based fallback (which refuses to
        # guess if the name is ambiguous), logging so the mismatch is visible.
        transition_logger.warning(
            f'TransitionMessage#{tm.pk}: recorded owning process '
            f'{owning_path!r} for background transition '
            f'{tm.transition_name!r} was not found in the process tree '
            f'(renamed or removed?); attempting name-based fallback.'
        )

    matches = _background_transitions_named(process, tm.transition_name)
    if len(matches) == 1:
        # Unambiguous — the legacy/pre-discriminator common case, safe to use.
        return matches[0]
    if len(matches) > 1:
        # Ambiguous AND no resolvable owner: do NOT guess. Raising _RestoreError
        # finalizes the row (stops retries) without running any side-effects —
        # far safer than running the wrong condition-disambiguated sibling.
        raise _RestoreError(
            f'background transition {tm.transition_name!r} matches '
            f'{len(matches)} transitions across the process tree and the '
            f'message has no resolvable owning_process_class '
            f'(recorded={tm.owning_process_class!r}); refusing to guess which '
            f'condition-disambiguated sibling to run. This is an in-flight row '
            f'enqueued before the owner discriminator existed, or whose owning '
            f'nested process was renamed/removed mid-flight. Drain in-flight '
            f'rows before refactoring a background action_name into shared '
            f'nested processes.'
        )
    return None  # zero matches -> generic not-found _RestoreError in _restore


def _find_background_transition_in_owner(process, action_name, owning_path):
    """Return the background transition named ``action_name`` declared on the
    process in the tree whose dotted class path equals ``owning_path``."""
    proc_path = f'{type(process).__module__}.{type(process).__name__}'
    if proc_path == owning_path:
        for transition in process.transitions:
            if (
                transition.action_name == action_name
                and getattr(transition, 'is_background', False)
            ):
                return transition
        # Class matched but it no longer declares the transition (renamed).
        return None
    for sub_process_class in process.nested_processes:
        sub_process = sub_process_class(state=process.state)
        found = _find_background_transition_in_owner(
            sub_process, action_name, owning_path
        )
        if found is not None:
            return found
    return None


def _background_transitions_named(process, action_name, _seen=None, _out=None):
    """All distinct ``is_background`` transitions named ``action_name`` across the
    process and its nested tree.

    De-duplicated by transition identity so a Process class legitimately reached
    via two nested paths (its class-level ``transitions`` are shared objects)
    counts once — otherwise the ambiguity check in ``_find_transition`` would
    false-positive on a reused sub-process.
    """
    if _seen is None:
        _seen, _out = set(), []
    for transition in process.transitions:
        if (
            transition.action_name == action_name
            and getattr(transition, 'is_background', False)
            and id(transition) not in _seen
        ):
            _seen.add(id(transition))
            _out.append(transition)
    for sub_process_class in process.nested_processes:
        sub_process = sub_process_class(state=process.state)
        _background_transitions_named(sub_process, action_name, _seen, _out)
    return _out
