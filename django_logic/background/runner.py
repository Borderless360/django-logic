"""Worker execution of a background transition.

``run_background_transition(transition_message_id)`` owns a single attempt at executing
a durable background transition. It runs the same way in:

* the pull worker loop (:mod:`django_logic.background.pull`), and
* sync mode, directly after enqueue in the same process.

Structure:

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
   * on success, writes ``target`` state (for ``BackgroundTransition``)
     and marks the TransitionMessage completed,
   * on failure, records the error and either leaves the row for retry
     or, at ``MAX_ERRORS``, writes ``failed_state`` and marks completed.

2. After the atomic block (best-effort):

   * success callbacks + ``next_transition`` (success path), or
   * failure callbacks (terminal-failure path).

Side-effect exceptions re-raise out of ``run_background_transition``
only in **sync mode**, so inline callers and tests can ``assertRaises``
directly. In **Pull mode** the runner swallows them once it has
recorded the outcome on the row (``errors_count`` + ``last_error``, or
``failed_state`` + completion). The claim's retry wait owns retries;
re-raising out of the worker loop would only add noise.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS, OperationalError, transaction
from django.utils import timezone

from django_logic import conf
from django_logic.background.models import TransitionMessage, db_safe_text
from django_logic.background.observability import set_sentry_context
from django_logic.background.serializers import deserialize_kwargs
from django_logic.background.transitions import BackgroundAction, BackgroundTransition
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
    except _StopRetry as exc:
        # The atomic block rolled back, so mark_as_completed could not run
        # inside it. Run it here, in its own statement, so the retry loop
        # stops picking the row up.
        _mark_unrestorable_completed(exc.transition_message_id, exc.reason)
        return
    except _NothingToDo:
        return

    # Best-effort hooks after the transaction commits.
    if outcome.terminal and outcome.succeeded and outcome.transition is not None:
        _run_success_hooks(outcome)
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


class _NothingToDo(Exception):
    """Internal signal: the row is already completed, missing, or locked
    by another worker. Caller should exit silently."""


class _StopRetry(Exception):
    """Internal signal: the row refers to a model or transition that no
    longer exists. The atomic block rolled back, so the outer handler marks
    the row completed in its own statement and retries stop. ``reason``
    describes the restore failure for ``last_error_message``."""

    def __init__(self, transition_message_id: int, reason: str = ''):
        self.transition_message_id = transition_message_id
        self.reason = reason


UNRESTORABLE_MARKER = '[unrestorable]'


def _mark_unrestorable_completed(transition_message_id: int, reason: str = '') -> None:
    """Mark an unrestorable row completed so no worker claims it again,
    and record why on ``last_error_message``. The
    note follows the ``'[superseded]'`` convention (see
    ``TransitionMessage.mark_as_superseded``) so an operator who reads the
    row later finds an explanation next to the completion.

    Runs as a single UPDATE outside the worker atomic block, which has
    already exited and rolled back. Durability is mode-dependent, with
    the same rule as ``TransitionMessage.stamp_attempt_started``; in sync
    mode a caller rollback also discards the enqueue INSERT, so no row
    survives to be retried and the stop-retry promise still holds.
    """
    now = timezone.now()
    # db_safe_text, not a plain slice: ``reason`` carries arbitrary
    # exception text, and PostgreSQL rejects a NUL or a lone surrogate —
    # this completion write must never be the statement that fails.
    note = db_safe_text(f'{UNRESTORABLE_MARKER} {reason or "restore failed"}')
    try:
        TransitionMessage.objects.filter(pk=transition_message_id, is_completed=False).update(
            is_completed=True,
            ended_in_failure=True,
            completed_at=now,
            last_error_message=note,
            last_error_dt=now,
            modified=now,  # .update() bypasses auto_now
        )
    except Exception as e:
        transition_logger.error(
            f'Failed to mark unrestorable TransitionMessage#{transition_message_id} '
            f'completed: {e}'
        )


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
            f'{transition_message.app_label}.{transition_message.model_name}#{transition_message.instance_id} '
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
    except _RestoreError:
        # No attempt ran here, so started_at (if any) belongs to an
        # abandoned attempt — don't record a misleading duration.
        transition_message.mark_as_completed(
            measure_duration=False, ended_in_failure=True)
        return None
    except Exception as exc:
        # Anything _restore did not treat as permanent: a consumer
        # ``process`` property raising, a corrupt instance_id, a short
        # database outage. Escaping here rolled the whole finalization back on
        # every run, so the safety net looped forever on this one row.
        # Completing it stops the loop, and the instance stays in its
        # in_progress_state, which is an implicit source of the same
        # transition.
        transition_logger.warning(
            f'detect_stuck: TransitionMessage#{transition_message.pk} could not be restored '
            f'({type(exc).__name__}: {exc}); completing it so the safety net '
            f'stops retrying. The instance stays in its in_progress_state; '
            f'run the transition again from there to move it on.',
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

    # Same state guard as the worker attempt path, and with the same result: a
    # manual fix wins over everything here, not only over the failed_state
    # write. When the guard covered the write alone, this safety net still ran
    # failure callbacks against an instance an operator had already fixed, and
    # completed the row with no ``[superseded]`` note to explain it.
    matches, expected, current = _state_guard_matches(transition, state)
    if not matches:
        note = (
            f'[superseded] detect_stuck state guard: expected {expected}, found '
            f'{current!r}. Something else moved the instance while this row '
            f'was pending, so failed_state and the failure callbacks are '
            f'skipped and the other state change wins. Earlier error: '
            f'{transition_message.last_error_message or "(none recorded)"}'
        )
        transition_logger.error(
            f'detect_stuck: TransitionMessage#{transition_message.pk} {transition.action_name} '
            f'{state.instance_key}: {note}'
        )
        transition_message.mark_as_superseded(note)
        return None

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
):
    """The one terminal-failure completion: write ``failed_state`` (when
    declared), then mark the row completed even if the write fails.
    Completing the row is what stops the retry loop; a failed write is
    recorded on the row where an operator will see it.

    Returns the ``(transition, state, kwargs, exception)`` tuple the
    caller needs to run ``failure_callbacks`` after its atomic block
    commits and the row lock is released.
    """
    if transition.failed_state:
        write_error = write_failed_state(
            state, transition.failed_state,
            prefix=prefix, consequence=consequence,
        )
        if write_error is not None:
            transition_message.record_failure_side_effect_error(
                write_error, label='failed_state write')
    transition_message.mark_as_completed(
        measure_duration=measure_duration, ended_in_failure=True)
    return (transition, state, kwargs, exception)


def _run_failure_callbacks(transition, state, kwargs, exception) -> None:
    """Run a terminal row's ``failure_callbacks`` best-effort, *after* the
    finalizing atomic block has committed and released the row lock.

    Every terminal-failure path runs through here: a row that reached
    MAX_ERRORS during an attempt, and a row detect_stuck
    finalized. ``Callbacks.execute`` already swallows exceptions; the guard
    here also covers a malformed hook list.
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


def _run_atomic(transition_message_id: int) -> _Outcome:
    """One attempt, one atomic block. Read it top to bottom:

    lock the row -> decode the saved kwargs -> restore the instance and
    its transition -> stop if something else moved the instance -> run
    the side-effects and the target write in one savepoint -> account
    the result on the row.

    Invariant: everything that must survive together lives inside this
    atomic block — row lock, side-effects, the target state write, and
    either mark_as_completed (on success / terminal failure) or the
    errors_count increment (on retryable failure). Moving any of the
    mark_as_* / record_error calls out is what broke the unrestorable-row
    path (see _StopRetry). Don't do it.

    started_at is the one deliberate exception. It marks the attempt
    instead of accounting for it: other connections must see it while the
    attempt runs, and it must survive the attempt rolling back. The
    caller therefore stamps and commits it before this block opens.
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
        state = process.state

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
            try:
                _execute_attempt(instance, transition, state, kwargs)
            except Exception as error:
                return _handle_failure(
                    transition_message, transition, state, kwargs, error
                )
            return _handle_success(transition_message, transition, state, kwargs)
        finally:
            _transition_context.reset(token)


def _lock_uncompleted_row(transition_message_id: int) -> TransitionMessage:
    """Lock the row for this attempt, or raise ``_NothingToDo``.

    Already completed / missing, and held by another worker, are the
    documented exit-silently cases: the row stays claimable and a later
    claim retries it, so nothing is lost by skipping.
    """
    try:
        return (
            TransitionMessage.objects
            .select_for_update(nowait=True)
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

    Returns ``(triple, None)`` on success and ``(None, error)`` on a
    failure that deserves normal error accounting. Raises ``_StopRetry``
    for the permanent classes (model uninstalled, row gone, transition
    renamed), where retrying can never help.
    """
    try:
        # In a savepoint for the same reason as the decode: _restore reads
        # the instance and (for user kwargs) the user table, so a
        # DatabaseError must break only the savepoint. Otherwise the error
        # bookkeeping cannot run.
        with transaction.atomic():
            return _restore(transition_message), None
    except _RestoreError as exc:
        transition_logger.error(
            f'TransitionMessage#{transition_message.pk} cannot be restored: {exc}. '
            f'Marking completed to stop retries.'
        )
        # Don't mark_as_completed() here — we're inside an atomic
        # block that will roll back when we exit. The outer handler
        # in run_background_transition() performs the mark in a
        # fresh statement so the stop-retry flag actually persists.
        raise _StopRetry(transition_message.pk, str(exc)) from exc
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
    transition_message, transition, state, kwargs
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
    note = (
        f'[superseded] worker state guard: expected {expected}, '
        f'found {current!r} — the instance was moved by something '
        f'else while this transition was pending. Side-effects '
        f'skipped; the external state change wins.'
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
        if not isinstance(transition, BackgroundAction):
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
    """Count a restore failure that is not one of the permanent kinds.

    There is no transition object to fail through, so this is thinner than
    ``_handle_failure``. It records the error, retries while retries remain,
    and at ``MAX_ERRORS`` completes the row so the retry loop stops. Nothing
    was restored, so no ``failed_state`` is written and the instance stays in
    its ``in_progress_state``. That state is an implicit source of the same
    transition, so an operator can run it again, and the completed row carries
    the reason.
    """
    transition_message.record_error(error)
    transition_logger.error(
        f'TransitionMessage#{transition_message.pk} restore raised '
        f'{type(error).__name__}: {error}',
        exc_info=True,
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
    # The target write happens inside the attempt savepoint in _run_atomic,
    # so by here the state is already committed-pending and this only has
    # to close the row out.
    transition_message.mark_as_completed()
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
) -> _Outcome:
    transition_message.record_error(error)
    transition_logger.error(
        f'{kwargs.get("tr_id")} {TransitionEventType.FAIL.value}: '
        f'{type(error).__name__}: {error}',
        exc_info=True,
    )

    exhausted = transition_message.errors_count >= conf.max_errors()
    terminal = exhausted or _failure_is_permanent(transition, error)
    if terminal and not exhausted:
        transition_logger.info(
            f'{kwargs.get("tr_id")} {transition.action_name} failed '
            f'permanently ({type(error).__name__}); not retried.'
        )
    if terminal:
        # The completion comes AFTER record_error, so letting a rejected
        # failed_state write propagate would roll that error back and hold
        # errors_count one below MAX_ERRORS forever. The instance then stays
        # in its in_progress_state, which is an implicit source of the same
        # transition, so it can be run again.
        _complete_terminal_failure(
            transition_message, transition, state, kwargs, error,
            prefix=f'{kwargs.get("tr_id")}',
            consequence=(
                f'Completing the row anyway so it stops retrying. The instance '
                f'stays in {transition.in_progress_state!r}; run the transition '
                f'again from there to move it on.'
            ),
            measure_duration=True,
        )
    # Not terminal: leave uncompleted → claimable again after the retry wait.
    return _Outcome(
        terminal=terminal,
        succeeded=False,
        exception=error,
        transition=transition,
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
    no_retry_on = getattr(transition, 'no_retry_on', ())
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


class _RestoreError(Exception):
    """The TransitionMessage refers to a model/instance/transition that
    no longer exists. The row is marked completed to stop the retry loop.
    """


def _state_guard_matches(transition, state) -> tuple[bool, str, str]:
    """Does the persisted state still match what enqueue left behind?

    * Transition with ``in_progress_state`` — enqueue wrote it, so the
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


def _restore(transition_message: TransitionMessage):
    """Resolve ``(instance, process, transition)`` from a TransitionMessage row."""
    # The key columns name the concrete model; proxy_model_label names the
    # class the caller drove. Restore that class, so proxy methods and
    # overrides stay visible to side-effects and callbacks. Rows written
    # before the column existed record the driving class in model_name.
    recorded_model = (
        transition_message.proxy_model_label
        or f'{transition_message.app_label}.{transition_message.model_name}'
    )
    try:
        model = apps.get_model(recorded_model)
    except LookupError as exc:
        raise _RestoreError(f'model {recorded_model} not installed') from exc

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

    recorded_path = (transition_message.kwargs or {}).get('process_class')
    try:
        process = getattr(instance, transition_message.process_name)
    except AttributeError:
        # Fall back to process_class stored in kwargs, if any.
        if not recorded_path:
            raise _RestoreError(
                f'instance has no process named {transition_message.process_name!r} and '
                f'no process_class stored on the message'
            )
        process = None
    else:
        # Check that the attribute resolved to the class enqueue recorded.
        # Every Process defaults to process_name='process', so two processes
        # can share the name and the worker would restore the wrong class and
        # run side-effects the caller never asked for. The recorded class wins.
        if recorded_path:
            resolved_path = f'{type(process).__module__}.{type(process).__name__}'
            if resolved_path != recorded_path:
                transition_logger.warning(
                    f'TransitionMessage#{transition_message.pk}: process_name '
                    f'{transition_message.process_name!r} resolved to {resolved_path}, but '
                    f'the message was enqueued by {recorded_path}; using '
                    f'the recorded class.'
                )
                process = None
    if process is None:
        try:
            process = _load_process_from_path(instance, recorded_path, transition_message)
        except Exception as exc:
            # Fail closed through the stop-retry path: the row completes as
            # unrestorable, with no side-effects and no state write. A bare
            # ImportError would escape _run_atomic, which catches only
            # _RestoreError, and roll the attempt back with errors_count
            # unchanged, so the claim filter would offer the row forever.
            # Let pending rows complete before you rename a Process class.
            raise _RestoreError(
                f'recorded process_class {recorded_path!r} could not be '
                f'loaded: {exc}'
            ) from exc

    transition = _find_transition(process, transition_message)
    if transition is None:
        raise _RestoreError(
            f'transition {transition_message.transition_name!r} not found on process '
            f'{type(process).__module__}.{type(process).__name__}'
        )
    return instance, process, transition


def _load_process_from_path(instance, dotted: str, transition_message: TransitionMessage):
    module_path, class_name = dotted.rsplit('.', 1)
    module = importlib.import_module(module_path)
    process_class = getattr(module, class_name)
    if not transition_message.field_name:
        # Enqueue has recorded the bound field since 0.4; a row without one
        # cannot be restored to a known field, and guessing 'state' could
        # drive the wrong machine on a multi-process model.
        raise _RestoreError(
            f'TransitionMessage {transition_message.pk} has no field_name; it predates 0.4 '
            f'or was created by hand'
        )
    return process_class(field_name=transition_message.field_name, instance=instance)


def _find_transition(process, transition_message: TransitionMessage):
    """Resolve the exact background transition a ``TransitionMessage`` names.

    One walk over the ``nested_processes`` tree (``_iter_process_tree``
    supplies the cycle guard — A nesting B nesting A is legal). A
    transition is a candidate when its ``action_name`` matches and it is
    a background one. Only background transitions: the worker never
    restores a synchronous transition, and the lookup skips state
    membership on purpose — the instance sits in ``in_progress_state``,
    which is not in the transition's declared ``sources``.

    Enqueue records the process class that declared the transition on
    ``transition_message.owning_process_class``. A candidate declared on
    that class wins at once — that is how an ``action_name`` shared by
    nested processes resolves to the one enqueue chose. The owner is
    blank only for rows enqueued before the column existed, or for the
    rare row created outside the Process entrypoint.

    When the owner is blank or did not match (the class was renamed or
    removed, or no longer declares the transition), the name decides —
    but only when it matches exactly one background transition in the
    whole tree. For an ambiguous name we refuse to guess and raise
    ``_RestoreError``: running a random sibling could run the wrong
    integration's side-effects or strand the instance. The row is
    finalized, retries stop, and no side-effects run.
    """
    action_name = transition_message.transition_name
    owning_path = (transition_message.owning_process_class or '').strip()
    seen, matches = set(), []
    for process_cls in _iter_process_tree(type(process)):
        proc_path = f'{process_cls.__module__}.{process_cls.__name__}'
        for transition in process_cls.transitions:
            if (
                transition.action_name == action_name
                and getattr(transition, 'is_background', False)
            ):
                if proc_path == owning_path:
                    return transition
                if id(transition) not in seen:
                    # By identity: a Process class reached through two
                    # nested paths shares its class-level transition
                    # objects, and a shared object is one match, not two.
                    seen.add(id(transition))
                    matches.append(transition)
    if owning_path:
        transition_logger.warning(
            f'TransitionMessage#{transition_message.pk}: recorded process class '
            f'{owning_path!r} does not declare background transition '
            f'{transition_message.transition_name!r} (class renamed or removed, '
            f'or the transition moved); attempting name-based fallback.'
        )
    if len(matches) == 1:
        # One match, so the name is enough: the common case for older rows.
        return matches[0]
    if len(matches) > 1:
        raise _RestoreError(
            f'background transition {transition_message.transition_name!r} matches '
            f'{len(matches)} transitions across the process tree and the '
            f'message has no resolvable owning_process_class '
            f'(recorded={transition_message.owning_process_class!r}); refusing to guess which '
            f'one to run. The row was enqueued before the owner column '
            f'existed, or its nested process class was renamed or removed '
            f'while the row was pending. Let pending rows complete before you '
            f'move a background action_name onto shared nested processes.'
        )
    return None  # zero matches -> generic not-found _RestoreError in _restore
