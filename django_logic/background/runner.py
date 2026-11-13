"""Worker execution of a background transition.

``run_background_transition(transition_message_id)`` owns a single attempt at executing
a durable background transition. It runs the same way in:

* the pull worker loop (:mod:`django_logic.background.pull`), and
* sync mode, directly after enqueue in the same process.

Default atomic execution:

1. One ``atomic`` block that:

   * locks the TransitionMessage row with ``select_for_update(nowait=True)``
     (another worker already holds it → raise ``OperationalError`` →
     caller exits silently),
   * restores the instance + transition,
   * verifies the instance is still in the state enqueue left behind
     (the *state guard*). On a mismatch the row completes as superseded
     and the side-effects are skipped, so a manual fix is never
     overwritten,
   * runs each side-effect in order **inside a savepoint**, so a failed
     attempt rolls back every side-effect write. The savepoint also
     keeps the outer transaction healthy when a side-effect raises
     ``DatabaseError``, so the error bookkeeping below still runs,
   * on success, writes the ``target`` state (when one is declared)
     and marks the TransitionMessage completed,
   * on ``Defer``, saves the next eligible time without counting an error,
   * on other failures, records the error and retries unless it is permanent or the error allowance is exhausted.
     Terminal failure checks the business state under its lock before writing ``failed_state`` and completing the job.

2. After the atomic block (best-effort):

   * success callbacks (success path), or
   * failure callbacks (terminal-failure path).

``atomic=False`` declarations use :mod:`django_logic.background.non_atomic`
to preserve provider receipts without an enclosing model transaction.

Side-effect exceptions re-raise out of ``run_background_transition``
only in **sync mode**, so inline callers and tests can ``assertRaises``
directly. In **Pull mode** the runner swallows them once it has
recorded the outcome on the row (``errors_count`` + ``last_error``, or
``failed_state`` + completion). The claim's retry wait owns retries;
re-raising out of the worker loop would only add noise.
"""
from __future__ import annotations

import importlib
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, OperationalError, transaction
from django.db.models import Q
from django.utils import timezone

from django_logic import conf
from django_logic.background.models import TransitionMessage, db_safe_text
from django_logic.background.observability import set_sentry_context
from django_logic.background.serializers import deserialize_kwargs
from django_logic.background.transitions import BackgroundTransition
from django_logic.commands import _run_in_savepoint, write_failed_state
from django_logic.logger import TransitionEventType, transition_logger
from django_logic.process import _iter_process_tree, _transition_context


@dataclass
class _Outcome:
    """What the worker's atomic block produced — drives best-effort hooks."""

    terminal: bool  # Work is done (target, failed, or nothing to run)
    succeeded: bool
    exception: BaseException | None = None
    transition: BackgroundTransition | None = None
    state_obj: Any = None
    kwargs: dict | None = None


def run_background_transition(transition_message_id: int) -> None:
    """Run a single attempt at the transition identified by ``transition_message_id``.

    Designed to be call-compatible from both the pull worker loop and an
    inline sync dispatcher.
    """
    # Committed BEFORE the attempt's atomic block, and deliberately not rolled
    # back with it (see TransitionMessage.stamp_attempt_started). The stuck
    # report and the retry classification both read this stamp, and inside
    # the atomic block it was invisible to them.
    if not TransitionMessage.stamp_attempt_started(transition_message_id):
        # Another attempt holds the row, or the row is completed or gone. Both
        # are exit-silently cases: the row stays claimable, so a later claim
        # retries it and skipping loses nothing.
        transition_logger.info(
            f'TransitionMessage#{transition_message_id}: another attempt holds '
            f'the row (or it is already completed); skipping this dispatch.'
        )
        return
    try:
        outcome = _run_atomic(transition_message_id)
        if isinstance(outcome, _NonAtomicAttempt):
            from django_logic.background.non_atomic import run_non_atomic
            outcome = run_non_atomic(transition_message_id)
    except _NothingToDo:
        return

    # Best-effort hooks after the transaction commits.
    if outcome.terminal and outcome.succeeded and outcome.transition is not None:
        _run_success_hooks(outcome)
        if outcome.transition.background_effects:
            from django_logic.background.effects import dispatch_effect_jobs
            dispatch_effect_jobs(transition_message_id)
    elif outcome.terminal and not outcome.succeeded and outcome.transition is not None:
        _run_failure_callbacks(
            outcome.transition, outcome.state_obj,
            outcome.kwargs, outcome.exception,
        )

    if outcome.exception is not None:
        # Sync mode propagates so the inline caller and tests can react.
        # Pull mode must NOT re-raise. The outcome is already recorded on
        # the row and the claim's retry wait owns retries; re-raising out
        # of the worker loop would only add noise.
        if conf.sync_mode():
            raise outcome.exception


class _NonAtomicAttempt:
    """Run this declaration without an enclosing model transaction."""


class _NothingToDo(Exception):
    """Internal signal: the row is already completed, missing, or locked
    by another worker. Caller should exit silently."""


UNRESTORABLE_MARKER = '[unrestorable]'


def finalize_stuck_attempt(transition_message_id: int) -> bool:
    """Force a stuck (``errors_count >= MAX_ERRORS``, uncompleted) row
    into a terminal state (``failed_state`` + ``mark_as_completed``).

    Called by ``detect_stuck_transitions``. If a worker holds the row we exit
    silently, because the running attempt finalizes it on its own. Otherwise we
    restore the transition, run the terminal-failure steps, and mark the row
    completed.

    Returns True if the row was finalized, False if skipped.
    """
    hooks = None
    with transaction.atomic():
        try:
            transition_message = (
                TransitionMessage.objects
                .select_for_update(nowait=True)
                .get(pk=transition_message_id, is_completed=False)
            )
        except TransitionMessage.DoesNotExist:
            return False
        except OperationalError:
            transition_logger.info(
                f'detect_stuck: TransitionMessage#{transition_message_id} locked by a '
                f'worker; deferring finalization'
            )
            return False

        transition_logger.warning(
            f'Stuck transition: TransitionMessage#{transition_message.pk} '
            f'{transition_message.recorded_model_label}#{transition_message.instance_id} '
            f'{transition_message.transition_name} queue={transition_message.queue_name} '
            f'errors={transition_message.errors_count} '
            f'last_error={transition_message.last_error_message!r}; forcing terminal state'
        )
        # Build an exception from the stored last_error_message so the failure
        # callbacks see the same kind of error the last attempt would have
        # passed them.
        err = RuntimeError(
            f'[detect_stuck] {transition_message.last_error_message or "transition stuck"}'
        )
        hooks = _finalize_stuck_row(transition_message, err)

    # Run failure_callbacks after the atomic commits (best-effort).
    if hooks is not None:
        _run_failure_callbacks(*hooks)
    return True


def _finalize_stuck_row(
    transition_message: TransitionMessage,
    exception: BaseException,
):
    """The stuck finalizer's terminal path.

    Must run inside the caller's atomic block, with the row already locked.
    It restores the transition, applies the state guard, and ends in the
    same shared completion as the worker attempt path
    (``_complete_terminal_failure``).

    If the transition cannot be restored, because the model is uninstalled or
    the transition was renamed, we still mark the row completed so retries
    stop. The failed_state write is skipped: there is nothing to write it on.

    Returns the ``(transition, state, kwargs, exception)`` tuple the caller
    needs to run ``failure_callbacks`` *after* its atomic block commits, so
    the callbacks do not run while the row lock is held. Returns ``None`` when
    the row could not be restored and there is nothing to call.
    """
    try:
        # In a savepoint: _restore reads the instance, and restore_user reads
        # the user table. A DatabaseError there would break the caller's
        # transaction and take the completion below down with it.
        with transaction.atomic():
            _, process, transition = _restore(transition_message)
    except Exception as exc:
        # Completion stops repeated restore failures without changing business state.
        transition_logger.warning(
            f'detect_stuck: TransitionMessage#{transition_message.pk} could not be restored '
            f'({type(exc).__name__}: {exc}); completing it so the safety net '
            f'stops retrying. Business state is unchanged; check the restore '
            f'error before submitting this transition again.',
            exc_info=True,
        )
        transition_message.mark_as_completed(
            measure_duration=False, ended_in_failure=True)
        return None

    kwargs, decode_error = _decode_kwargs(transition_message)
    if decode_error is not None:
        # kwargs that no longer decode must not block the finalization:
        # failed_state and the completion still land, so retries stop.
        transition_logger.warning(
            f'detect_stuck: TransitionMessage#{transition_message.pk} kwargs failed to decode '
            f'({type(decode_error).__name__}: {decode_error}); finalizing with empty kwargs.'
        )
    state = process.state

    # A safety-net finalization is not a worker attempt. started_at belongs to
    # the abandoned attempt, so measuring from it would inflate duration_ms.
    return _complete_terminal_failure(
        transition_message, transition, state, kwargs, exception,
        prefix='detect_stuck:',
        consequence='Completing the row anyway so it stops retrying.',
        measure_duration=False,
    )


def _complete_terminal_failure(
    transition_message: TransitionMessage,
    transition,
    state,
    kwargs: dict,
    exception: BaseException,
    *,
    prefix: str,
    consequence: str,
    measure_duration: bool,
    using=None,
):
    """Complete a failed job after checking the business row under its lock.

    A newer business state wins. If its state cannot be checked under the
    bounded lock, record that problem and complete the failed job without changing
    the business state or running failure callbacks. A rejected state write
    after the check still preserves the existing callback behavior.

    Returns the ``(transition, state, kwargs, exception)`` tuple the
    caller needs to run ``failure_callbacks`` after its atomic block
    commits and the row lock is released, or ``None`` when callbacks must
    not run.
    """
    try:
        # A failed business-row check must not discard the job's recorded error.
        with transaction.atomic(using=using or state.instance._state.db or DEFAULT_DB_ALIAS), _protect_completion_state(transition, state):
            matches, expected, current = _state_guard_matches(transition, state)
            write_error = None
            if matches and transition.failed_state:
                write_error = write_failed_state(
                    state, transition.failed_state,
                    prefix=prefix, consequence=consequence, using=using,
                )
    except Exception as error:
        transition_message.record_failure_side_effect_error(
            error, label='failed_state protection',
        )
        transition_logger.warning(
            f'{prefix} Completing the failed job without changing the business '
            f'state or running failure callbacks because its state could not '
            f'be checked under the row lock: {type(error).__name__}: {error}'
        )
        transition_message.mark_as_completed(
            measure_duration=measure_duration, ended_in_failure=True)
        return None
    if not matches:
        note = (
            f'[superseded] {prefix} failure completion: expected {expected}, '
            f'found {current!r}. The business state changed, so failed_state '
            f'and failure callbacks are skipped. Earlier error: '
            f'{transition_message.last_error_message or "(none recorded)"}'
        )
        transition_logger.warning(note)
        transition_message.mark_as_superseded(note)
        return None
    if write_error is not None:
        transition_message.record_failure_side_effect_error(
            write_error, label='failed_state write')
    transition_message.mark_as_completed(
        measure_duration=measure_duration, ended_in_failure=True)
    return (transition, state, kwargs, exception)


def _run_failure_callbacks(transition, state, kwargs, exception) -> None:
    """Run a terminal row's ``failure_callbacks`` after its execution block exits.

    Pull workers have committed by this point. Inline callers may still hold an outer transaction.

    Restoration failures and jobs whose business state cannot be checked do not run these callbacks.
    ``Callbacks.execute`` already swallows ordinary exceptions; this guard also covers a malformed hook list.
    """
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


def _run_atomic(transition_message_id: int) -> _Outcome | _NonAtomicAttempt:
    """Run an atomic declaration, or return the explicit non-atomic choice.

    Default execution uses one atomic block. Read it top to bottom:

    lock the row -> decode the saved kwargs -> restore the instance and
    its transition -> stop if something else moved the instance -> run
    the side-effects and the target write in one savepoint -> account
    the result on the row.

    Invariant: everything that must survive together lives inside this
    atomic block — row lock, side-effects, the target state write, and
    either mark_as_completed (on success / terminal failure) or the
    errors_count increment (on retryable failure). The same transaction
    completes a job whose model or transition no longer exists.

    The start times and attempt count are deliberate exceptions.
    Other connections must see them while the attempt runs.
    They must survive a rollback, so the caller commits them before this block opens.
    """
    with transaction.atomic():
        transition_message = _lock_uncompleted_row(transition_message_id)

        # Per-transition monitoring identity (Sentry transaction name + tags);
        # best-effort, no-op without sentry-sdk. See observability.py.
        set_sentry_context(transition_message)

        kwargs, decode_error = _decode_kwargs(transition_message)

        restored, restore_error = _restore_for_attempt(transition_message)
        if restore_error is not None:
            return _handle_restore_failure(transition_message, restore_error)
        instance, process, transition = restored
        if not transition.atomic:
            return _NonAtomicAttempt()
        state = process.state

        try:
            with state.protected() if (transition.target is not None or transition.failed_state) else nullcontext():
                return _run_atomic_state(transition_message, transition, state, instance, kwargs, decode_error)
        except Exception as error:
            return _handle_failure(transition_message, transition, state, kwargs, error)


def _run_atomic_state(transition_message, transition, state, instance, kwargs, decode_error):
    superseded = _superseded_outcome(
        transition_message, transition, state, kwargs
    )
    if superseded is not None:
        return superseded

    token = _transition_context.set(
        {
            'root_id': kwargs.get('root_id'),
            'tr_id': kwargs.get('tr_id'),
        }
    )
    try:
        transition_logger.info(
            f'{kwargs.get("tr_id")} Execute Start '
            f'{transition.action_name} {state.instance_key} '
            f'queue={transition_message.queue_name}'
        )
        if decode_error is not None:
            return _handle_failure(
                transition_message, transition, state, kwargs, decode_error
            )
        def execute_and_complete():
            _execute_attempt(instance, transition, state, kwargs)
            with _protect_completion_state(transition, state):
                if transition.target is None:
                    superseded = _superseded_outcome(
                        transition_message, transition, state, kwargs, after_side_effects=True,
                    )
                    if superseded is not None:
                        return superseded
                return _handle_success(transition_message, transition, state, kwargs)
        try:
            return _run_in_savepoint(
                instance._state.db or DEFAULT_DB_ALIAS, execute_and_complete, require_commit=True,
            )
        except Exception as error:
            transition_message.refresh_from_db()
            instance.refresh_from_db(fields=[state.field_name])
            return _handle_failure(
                transition_message, transition, state, kwargs, error
            )
    finally:
        _transition_context.reset(token)


def _lock_uncompleted_row(transition_message_id: int, *, using=None) -> TransitionMessage:
    """Lock the row for this attempt, or raise ``_NothingToDo``.

    Already completed / missing, and held by another worker, are the
    documented exit-silently cases: the row stays claimable and a later
    claim retries it, so nothing is lost by skipping.
    """
    try:
        return (
            TransitionMessage.objects.using(using)
            .select_for_update(nowait=True)
            .filter(Q(eligible_at__isnull=True) | Q(eligible_at__lte=timezone.now()))
            .get(pk=transition_message_id, is_completed=False)
        )
    except TransitionMessage.DoesNotExist as exc:
        transition_logger.info(
            f'TransitionMessage#{transition_message_id} already completed or missing; '
            f'nothing to do'
        )
        raise _NothingToDo() from exc
    except OperationalError as exc:
        transition_logger.info(
            f'TransitionMessage#{transition_message_id} locked by another worker; '
            f'skipping this attempt'
        )
        raise _NothingToDo() from exc


def _decode_kwargs(transition_message) -> 'tuple[dict, BaseException | None]':
    """Decode the kwargs saved at enqueue. Returns ``(kwargs, error)``.

    A decode failure counts like any other attempt failure. Raised here it
    would escape before record_error, leaving errors_count at 0, and
    the claim filter would offer the row to workers forever. So we
    return the error and the caller passes it to _handle_failure once the
    row is restored. The savepoint keeps the outer transaction healthy when
    the failure is a DatabaseError, so the error bookkeeping still runs.
    """
    try:
        with transaction.atomic():
            kwargs = deserialize_kwargs(transition_message.kwargs)
    except Exception as exc:
        return {'context': {}}, exc
    # Mirror the synchronous path (Transition._init_transition_context):
    # side-effects/callbacks may read a framework-provided ``context``
    # dict. serialize_kwargs drops it at enqueue, so rebuild it here —
    # otherwise a side-effect declared as ``def fn(instance, context,
    # **kwargs)`` works synchronously but raises in background mode.
    kwargs.setdefault('context', {})
    return kwargs, None


def _restore_for_attempt(transition_message):
    """Rebuild ``(instance, process, transition)`` from the row.

    Return ``(triple, None)`` on success or ``(None, error)`` on failure.
    The caller records the outcome while it still holds the job row.
    """
    try:
        # In a savepoint for the same reason as the decode: _restore reads
        # the instance and (for user kwargs) the user table, so a
        # DatabaseError must break only the savepoint. Otherwise the error
        # bookkeeping cannot run.
        with transaction.atomic():
            return _restore(transition_message), None
    except Exception as exc:
        # _restore raises _RestoreError only for the permanent failures.
        # Everything else — a consumer ``process`` property raising, a
        # corrupt instance_id, a temporary database error — used to escape
        # the worker with errors_count still 0, so the row stayed claimable
        # forever. Count it like any other attempt failure:
        # temporary causes get their retries, permanent ones reach
        # MAX_ERRORS and stop.
        return None, exc


def _superseded_outcome(
    transition_message, transition, state, kwargs, *, after_side_effects=False,
) -> '_Outcome | None':
    """The state guard: stop if something else moved the instance.

    The worker restores the transition by name and deliberately skips the
    source-state check. Without this guard it would overwrite any state
    change made while the row was pending, including a manual fix. Retries
    span RETRY_MINUTES x MAX_ERRORS, so that clash happens in production.

    Returns the superseded ``_Outcome``, or ``None`` when the state still
    matches and the attempt should proceed.
    """
    matches, expected, current = _state_guard_matches(transition, state)
    if matches:
        return None
    skipped = (
        'Final state write skipped; earlier side effects may have completed.'
        if after_side_effects else 'Side-effects skipped.'
    )
    note = (
        f'[superseded] worker state guard: expected {expected}, '
        f'found {current!r} — the instance was moved by something '
        f'else while this transition was pending. {skipped} '
        f'The external state change wins.'
    )
    transition_logger.error(
        f'{kwargs.get("tr_id")} TransitionMessage#{transition_message.pk} '
        f'{transition.action_name} {state.instance_key}: {note}'
    )
    transition_message.mark_as_superseded(note)
    return _Outcome(terminal=True, succeeded=False)


def _execute_attempt(instance, transition, state, kwargs) -> None:
    """Run the side-effects, then the target write, in ONE savepoint, so an
    attempt writes everything or nothing. Raises whatever the attempt raised.

    The savepoint does two jobs. A failed attempt rolls back every
    side-effect write, and a DatabaseError from a side-effect breaks only the
    savepoint, so record_error and mark_as_completed still work in the outer
    transaction. Without it, a database error here made record_error itself
    raise TransactionManagementError: the error was never recorded, the row
    was sent to the queue forever, and it blocked every later background
    transition on the instance.

    It runs through _run_in_savepoint on the INSTANCE's alias: set_state
    routes its write to the instance's connection, so a savepoint opened
    on DEFAULT would guard the wrong one.

    require_commit, because the caller records the work as done. A
    side-effect that raises a database error and swallows it
    (`try: obj.save() except IntegrityError: pass` with no nested atomic)
    makes Django discard the savepoint and nothing propagates. The attempt
    then returns as a success but committed none of its writes, so we count
    it as the failure it is.
    """
    def _attempt():
        for command in transition.side_effects.commands:
            transition_logger.info(
                f'{kwargs.get("tr_id")} '
                f'{TransitionEventType.SIDE_EFFECT.value} '
                f'{getattr(command, "__name__", repr(command))}'
            )
            command(instance, **kwargs)
        # The target write belongs INSIDE the attempt savepoint, because it
        # is part of the attempt. A write the database rejects (a CHECK
        # constraint, a pre_save receiver, a save() override, a column
        # length) must roll the attempt back and count like any other
        # failure. Outside the savepoint the exception escaped the outer
        # atomic block and took record_error with it: errors_count stayed 0,
        # so the row was retried forever and no safety net could stop it.
        if transition.target is not None:
            state.set_state(transition.target)
            transition_logger.info(
                f'{kwargs.get("tr_id")} '
                f'{TransitionEventType.SET_STATE.value} '
                f'{transition.target}'
            )

    _run_in_savepoint(
        instance._state.db or DEFAULT_DB_ALIAS, _attempt,
        require_commit=True,
    )


def _handle_restore_failure(
    transition_message: TransitionMessage, error: BaseException,
) -> _Outcome:
    """Complete missing work, or count a temporary restore failure for retry."""
    if isinstance(error, _RestoreError):
        now = timezone.now()
        TransitionMessage.objects.using(transition_message._state.db).filter(
            pk=transition_message.pk,
        ).update(
            is_completed=True, ended_in_failure=True, completion_kind='failed',
            completed_at=now, last_error_dt=now, modified=now,
            last_error_message=db_safe_text(
                f'{UNRESTORABLE_MARKER} {str(error) or "restore failed"}'),
        )
        transition_logger.error(
            f'TransitionMessage#{transition_message.pk} cannot be restored: {error}. '
            f'Marking completed to stop retries.'
        )
        return _Outcome(terminal=True, succeeded=False)

    transition_message.record_error(error)
    transition_logger.error(
        f'TransitionMessage#{transition_message.pk} restore raised '
        f'{type(error).__name__}: {error}',
        exc_info=(type(error), error, error.__traceback__),
    )
    if transition_message.errors_count < conf.max_errors():
        return _Outcome(terminal=False, succeeded=False, exception=error)

    transition_logger.error(
        f'TransitionMessage#{transition_message.pk} restore failed {transition_message.errors_count} times; '
        f'completing the row so it stops retrying. No failed_state could be '
        f'written, so the instance stays in its in_progress_state; run the '
        f'transition again from there to move it on.'
    )
    transition_message.mark_as_completed(
        measure_duration=False, ended_in_failure=True)
    return _Outcome(terminal=True, succeeded=False, exception=error)


def _handle_success(
    transition_message: TransitionMessage,
    transition: BackgroundTransition,
    state,
    kwargs: dict,
) -> _Outcome:
    # The caller writes the target in the same transaction as this completion.
    transition_message.mark_as_completed()
    if transition_message.background_effect_names:
        from django_logic.background.effects import save_effect_jobs
        save_effect_jobs(transition_message, transition, state, kwargs)
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
    transition_message: TransitionMessage,
    transition: BackgroundTransition,
    state,
    kwargs: dict,
    error: BaseException,
    *, using=None,
) -> _Outcome:
    from django_logic.background.exceptions import Defer
    # A record another transaction holds for longer than the row lock wait
    # is an error like any other. It counts toward the retry limit, so a
    # job stuck behind an abandoned transaction ends up reported instead
    # of waiting quietly forever.
    if isinstance(error, Defer):
        transition_message.eligible_at = (
            error.until if settings.USE_TZ else timezone.make_naive(error.until, timezone.get_default_timezone())
        )
        transition_message.deferral_reason = db_safe_text(error.reason)
        transition_message.deferral_count += 1
        transition_message.last_deferred_at = timezone.now()
        transition_message.last_deferred_until = transition_message.eligible_at
        transition_message.last_deferral_reason = transition_message.deferral_reason
        transition_message.save(using=using or transition_message._state.db,
                                update_fields=['eligible_at', 'deferral_reason', 'deferral_count',
                                               'last_deferred_at', 'last_deferred_until',
                                               'last_deferral_reason', 'modified'])
        return _Outcome(terminal=False, succeeded=False)
    transition_message.eligible_at = None
    transition_message.deferral_reason = ''
    transition_message.save(using=using or transition_message._state.db, update_fields=['eligible_at', 'deferral_reason'])
    transition_message.record_error(error)
    transition_logger.error(
        f'{kwargs.get("tr_id")} {TransitionEventType.FAIL.value} '
        f'action={transition.action_name} instance={state.instance_key} '
        f'job={transition_message.pk}: '
        f'{type(error).__name__}: {error}',
        exc_info=(type(error), error, error.__traceback__),
    )

    exhausted = transition_message.errors_count >= conf.max_errors()
    terminal = exhausted or _failure_is_permanent(transition, error)
    if terminal and not exhausted:
        transition_logger.info(
            f'{kwargs.get("tr_id")} {transition.action_name} failed '
            f'permanently ({type(error).__name__}); not retried.'
        )
    run_failure_hooks = True
    if terminal:
        # The completion comes AFTER record_error, so letting a rejected
        # failed_state write propagate would roll that error back and hold
        # errors_count one below MAX_ERRORS forever. The instance then stays
        # in its in_progress_state, which is an implicit source of the same
        # transition, so it can be run again.
        run_failure_hooks = _complete_terminal_failure(
            transition_message, transition, state, kwargs, error,
            prefix=f'{kwargs.get("tr_id")}',
            consequence=(
                f'Completing the row anyway so it stops retrying. The instance '
                f'stays in {transition.in_progress_state!r}; run the transition '
                f'again from there to move it on.'
            ),
            measure_duration=True,
            using=using,
        ) is not None
    # Not terminal: leave uncompleted → claimable again after the retry wait.
    return _Outcome(
        terminal=terminal,
        succeeded=False,
        exception=error,
        transition=transition if run_failure_hooks else None,
        state_obj=state,
        kwargs=kwargs,
    )


def _failure_is_permanent(transition, error: BaseException) -> bool:
    """Whether ``error`` says another attempt gets the same answer.

    True for :class:`PermanentFailure` (the raise site declares it) and for
    the exception types the transition lists in ``no_retry_on`` (the
    declaration declares it, for types the consumer does not control).
    """
    from django_logic.background.exceptions import PermanentFailure

    if isinstance(error, PermanentFailure):
        return True
    no_retry_on = transition.no_retry_on
    return bool(no_retry_on) and isinstance(error, no_retry_on)


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


class _RestoreError(Exception):
    """The TransitionMessage refers to a model/instance/transition that
    no longer exists. The row is marked completed to stop the retry loop.
    """


def _state_guard_matches(transition, state) -> tuple[bool, str, str]:
    """Does the persisted state still match what enqueue left behind?

    * Transition with ``in_progress_state`` — enqueue wrote it, so the
      instance must still be exactly there.
    * Transition without ``in_progress_state`` — the instance must
      still be in one of the declared sources.

    Returns ``(matches, expected_description, current_state)``.
    """
    if getattr(transition, 'is_independent_effect', False):
        return True, 'independent background effect', ''
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


def _protect_completion_state(transition, state):
    # Independent effects only complete their job; they never write business state.
    return nullcontext() if getattr(transition, 'is_independent_effect', False) else state.protected()


def _restore(transition_message: TransitionMessage):
    """Resolve ``(instance, process, transition)`` from a TransitionMessage row."""
    # The key columns name the concrete model; recorded_model_label names
    # the class that recorded the row. Restore that class, so proxy
    # methods and overrides stay visible to side-effects and callbacks.
    recorded_model = transition_message.recorded_model_label
    try:
        model = apps.get_model(recorded_model)
    except LookupError as exc:
        raise _RestoreError(f'model {recorded_model} not installed') from exc

    concrete = model._meta.concrete_model._meta
    if (transition_message.app_label, transition_message.model_name) != (concrete.app_label, concrete.model_name):
        raise _RestoreError('The saved model key must name the concrete model.')

    try:
        # _base_manager, not objects. A filtered default manager, such as one
        # that hides archived rows, raises DoesNotExist for an instance that
        # still exists. The restore-error path would then complete the row and
        # strand the instance in in_progress_state with no failed_state and no
        # retries. Framework code that reloads by pk must ignore default
        # manager filters, as Django does for related objects.
        instance = model._base_manager.get(pk=transition_message.instance_id)
    except model.DoesNotExist as exc:
        raise _RestoreError(
            f'{transition_message.app_label}.{transition_message.model_name}#{transition_message.instance_id} not found'
        ) from exc

    recorded_path = (transition_message.kwargs.get('process_class')
                     if isinstance(transition_message.kwargs, dict) else None)
    try:
        process = _load_process_from_path(instance, recorded_path, transition_message)
    except Exception as exc:
        raise _RestoreError(
            f'recorded process_class {recorded_path!r} could not be loaded: {exc}'
        ) from exc

    transition = _find_transition(process, transition_message)
    if transition_message.effect_name:
        try:
            transition = transition.get_background_effect(transition_message.effect_name).as_transition()
        except Exception as error:
            raise _RestoreError(f'background effect declaration cannot be restored: {error}') from error
    return instance, process, transition


def _load_process_from_path(instance, dotted: str, transition_message: TransitionMessage):
    from django_logic.process import Process

    if not isinstance(dotted, str) or not dotted or not transition_message.field_name:
        raise ValueError('A saved job requires process_class and field_name.')
    module_path, class_name = dotted.rsplit('.', 1)
    process_class = getattr(importlib.import_module(module_path), class_name)
    if not isinstance(process_class, type) or not issubclass(process_class, Process):
        raise ValueError('process_class must name a Process class.')
    if f'{process_class.__module__}.{process_class.__name__}' != dotted:
        raise ValueError('process_class must name the declared class, not an alias.')
    if process_class.process_name != transition_message.process_name:
        raise ValueError('The recorded process_name does not match process_class.')
    instance._meta.get_field(transition_message.field_name)
    return process_class(field_name=transition_message.field_name, instance=instance)


def _find_transition(process, transition_message: TransitionMessage):
    """Resolve the recorded declaring class and background action exactly."""
    action_name = (transition_message.parent_transition_name if transition_message.effect_name
                   else transition_message.transition_name)
    owning_path = transition_message.owning_process_class
    if not owning_path:
        raise _RestoreError('A saved job requires owning_process_class; refusing to guess.')
    for process_cls in _iter_process_tree(type(process)):
        if f'{process_cls.__module__}.{process_cls.__name__}' != owning_path:
            continue
        for transition in process_cls.transitions:
            if transition.action_name == action_name and transition.is_background:
                return transition
    raise _RestoreError(
        f'recorded owning_process_class {owning_path!r} does not declare '
        f'background transition {action_name!r} in the recorded process tree; refusing to guess.'
    )
