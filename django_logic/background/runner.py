"""Worker execution of a background transition.

``run_background_transition(transition_message_id)`` owns a single attempt at executing
a durable background transition. It runs the same way in:

* the Celery task wrapper (:mod:`django_logic.background.tasks`), and
* sync mode, directly after enqueue in the same process.

Structure:

1. One ``atomic`` block that:

   * locks the TransitionMessage row with ``select_for_update(nowait=True)``
     (another worker already holds it → raise ``OperationalError`` →
     caller exits silently),
   * restores the instance + transition,
   * verifies the instance is still in the state enqueue left behind
     (the *state guard* — on mismatch the row completes as superseded
     and side-effects are skipped, so a manual ops fix is never
     overwritten),
   * runs each side-effect in order **inside a savepoint** — a failed
     attempt rolls back every side-effect write (and keeps the outer
     transaction healthy even when the side-effect raised a genuine
     ``DatabaseError``, so the error bookkeeping below always works),
   * on success, writes ``target`` state (for ``BackgroundTransition``)
     and marks the TransitionMessage completed,
   * on failure, records the error and either leaves the row for retry
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
from datetime import timedelta
from typing import Any

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS, OperationalError, transaction
from django.utils import timezone

from django_logic.background import settings as bg_settings
from django_logic.background.models import TransitionMessage, db_safe_text
from django_logic.background.observability import set_sentry_context
from django_logic.background.serializers import deserialize_kwargs
from django_logic.background.transitions import BackgroundAction, BackgroundTransition
from django_logic.commands import _run_in_savepoint
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

    Designed to be call-compatible from both a Celery task and an
    inline sync dispatcher.
    """
    # Committed BEFORE the attempt's atomic block, and deliberately not
    # rolled back with it — see TransitionMessage.stamp_attempt_started.
    # This is the marker the watchdog and the retry starter's recency guard
    # read; written inside the atomic it was invisible to both.
    if not TransitionMessage.stamp_attempt_started(transition_message_id):
        # Row held by a live attempt, or already completed / gone — the
        # documented "exit silently" cases. The periodic starter
        # re-dispatches, so nothing is lost by skipping.
        transition_logger.info(
            f'TransitionMessage#{transition_message_id}: another attempt holds '
            f'the row (or it is already completed); skipping this dispatch.'
        )
        return
    try:
        outcome = _run_atomic(transition_message_id)
    except _StopRetry as exc:
        # The atomic block rolled back, so we couldn't mark_as_completed
        # from inside it. Do it here, in its own statement, to stop the
        # retry loop from picking the row up forever.
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
        # Sync mode propagates so the inline caller / tests can react.
        # Celery mode must NOT re-raise: the outcome is fully recorded on the
        # row, the periodic starter owns retries, and re-raising out of an
        # acks_late task risks broker redelivery + task-failure alert spam.
        from django_logic.background.dispatch import _current_mode
        if _current_mode() == bg_settings.EXECUTION_SYNC:
            raise outcome.exception


class _NothingToDo(Exception):
    """Internal signal: the row is already completed, missing, or locked
    by another worker. Caller should exit silently."""


class _StopRetry(Exception):
    """Internal signal: the row refers to a model/transition that no
    longer exists. The atomic block rolled back; the outer handler
    marks the row completed in its own statement so retries stop.
    ``reason`` carries the restore-failure description for the audit
    trail on ``last_error_message``."""

    def __init__(self, transition_message_id: int, reason: str = ''):
        self.transition_message_id = transition_message_id
        self.reason = reason


UNRESTORABLE_MARKER = '[unrestorable]'


def _mark_unrestorable_completed(transition_message_id: int, reason: str = '') -> None:
    """Mark an unrestorable row completed so the periodic starter stops
    re-dispatching it forever, recording WHY on ``last_error_message``
    (mirroring the ``'[superseded]'`` convention — see
    ``TransitionMessage.mark_as_superseded``) so an operator reading the
    row later isn't left with a completed row and no explanation.

    Runs as a single UPDATE outside the (already-exited, rolled-back)
    worker atomic block. Durability depends on the execution mode:

    * Celery mode — the worker runs as the top-level unit of work with no
      surrounding transaction, so this UPDATE autocommits and is durable.
      This is the path the original infinite-retry bug lived on.
    * Sync mode — enqueue (which created the row) and execute run in the
      same call stack and share the caller's transaction state. If the
      caller wraps the whole call in ``atomic()`` and later rolls back,
      this UPDATE rolls back too — but so does the enqueue INSERT, so there
      is no surviving row to re-dispatch and the stop-retry guarantee still
      holds. It is NOT a write that survives an *independent* parent
      rollback on its own; correcting an earlier docstring that claimed so.
    """
    now = timezone.now()
    # db_safe_text, not a bare slice: ``reason`` embeds arbitrary exception
    # text (a consumer module failing to import), and a NUL or lone surrogate
    # in it made this very UPDATE the statement PostgreSQL rejected — the row
    # then never completed and was re-dispatched forever, defeating the
    # stop-retry guarantee this function exists to provide.
    note = db_safe_text(f'{UNRESTORABLE_MARKER} {reason or "restore failed"}')
    try:
        TransitionMessage.objects.filter(pk=transition_message_id, is_completed=False).update(
            is_completed=True,
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


def abandon_timed_out_attempt(transition_message_id: int) -> bool:
    """Record a synthetic timeout error on a row whose current attempt
    has exceeded its declared ``timeout_seconds``.

    Skips rows currently held by a worker (``select_for_update(nowait)``
    → OperationalError) — we only act on abandoned attempts. When the
    error count reaches ``MAX_ERRORS`` the row is finalized in the same
    atomic block (failed_state + mark_as_completed) so the retry loop
    stops.

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
            transition_message = (
                TransitionMessage.objects
                .select_for_update(nowait=True)
                .get(pk=transition_message_id, is_completed=False)
            )
        except TransitionMessage.DoesNotExist:
            return False
        except OperationalError:
            transition_logger.info(
                f'watchdog: TransitionMessage#{transition_message_id} currently locked '
                f'by a worker; deferring abandon'
            )
            return False

        # Re-verify staleness against the row we just LOCKED. The candidate
        # scan runs unsynchronised, so a retry dispatch can stamp a fresh
        # started_at (stamp_attempt_started) in the window between the scan
        # and this lock. The one-charge guard below cannot catch that: the new
        # stamp is NEWER than the previous attempt's error, so the guard sees
        # "no error since this attempt started" and passes — and a healthy
        # attempt milliseconds old was charged a timeout, then terminalised
        # out from under its live worker at MAX_ERRORS. The scan's staleness
        # verdict is only a hint; the locked read is the truth.
        if (
            transition_message.started_at is None
            or transition_message.timeout_seconds is None
            or transition_message.started_at + timedelta(seconds=transition_message.timeout_seconds)
            >= timezone.now()
        ):
            transition_logger.info(
                f'watchdog: TransitionMessage#{transition_message.pk} is not stale when '
                f're-checked under the row lock (started_at={transition_message.started_at}, '
                f'timeout_seconds={transition_message.timeout_seconds}); a newer attempt '
                f'started since the scan. Leaving it alone.'
            )
            return False

        # One charge per attempt. If an error has been recorded since
        # this attempt started, the attempt already accounted for itself —
        # it is not abandoned, and charging again would burn a retry the
        # consumer never used. Left unguarded, the watchdog re-charged the
        # same attempt on every tick (errors_count 1 -> 2 -> 3 -> 4 with no
        # new attempts), so declaring timeout= made a transition strictly
        # LESS reliable than omitting it.
        if (
            transition_message.last_error_dt is not None
            and transition_message.started_at is not None
            and transition_message.last_error_dt >= transition_message.started_at
        ):
            transition_logger.info(
                f'watchdog: TransitionMessage#{transition_message.pk} exceeded '
                f'timeout_seconds={transition_message.timeout_seconds} but its attempt '
                f'already recorded an error at {transition_message.last_error_dt}; not '
                f'charging it twice.'
            )
            return False

        transition_logger.error(
            f'watchdog: TransitionMessage#{transition_message.pk} '
            f'{transition_message.app_label}.{transition_message.model_name}#{transition_message.instance_id} '
            f'{transition_message.transition_name} exceeded timeout_seconds='
            f'{transition_message.timeout_seconds}; recording timeout error'
        )
        err = TimeoutError(
            f'[watchdog timeout] attempt exceeded '
            f'timeout_seconds={transition_message.timeout_seconds}'
        )
        transition_message.record_error(err)

        max_errors = bg_settings.max_errors()
        if transition_message.errors_count >= max_errors:
            # Terminal. Finalize inside this same atomic — we already
            # hold the row lock so we cannot recurse through
            # finalize_stuck_attempt (deadlock).
            hooks = _finalize_terminal_from_watchdog(transition_message, err, source='watchdog')

    # Run failure_callbacks after the atomic commits and the row lock is
    # released (best-effort) — see _run_failure_callbacks.
    if hooks is not None:
        _run_failure_callbacks(*hooks)
    return True


def finalize_stuck_attempt(transition_message_id: int) -> bool:
    """Force a stuck (``errors_count >= MAX_ERRORS``, uncompleted) row
    into a terminal state (``failed_state`` + ``mark_as_completed``).

    Called by ``detect_stuck_transitions``. If the row is currently
    locked by a worker we exit silently — the running
    attempt will finalize on its own. Otherwise we restore the
    transition, run the terminal-failure sequence, and mark completed.

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

        transition_logger.error(
            f'Stuck transition: TransitionMessage#{transition_message.pk} '
            f'{transition_message.app_label}.{transition_message.model_name}#{transition_message.instance_id} '
            f'{transition_message.transition_name} queue={transition_message.queue_name} '
            f'errors={transition_message.errors_count} '
            f'last_error={transition_message.last_error_message!r}; forcing terminal state'
        )
        # Rehydrate an exception from the stored last_error_message so the
        # failure hooks see the same error shape the final in-task attempt
        # would have seen.
        err = RuntimeError(
            f'[detect_stuck] {transition_message.last_error_message or "transition stuck"}'
        )
        hooks = _finalize_terminal_from_watchdog(transition_message, err, source='detect_stuck')

    # Run failure_callbacks after the atomic commits (best-effort).
    if hooks is not None:
        _run_failure_callbacks(*hooks)
    return True


def _finalize_terminal_from_watchdog(
    transition_message: TransitionMessage,
    exception: BaseException,
    source: str,
):
    """Shared terminal-failure path for the watchdog / detect-stuck tasks.

    Must run inside the caller's atomic block, with the row already
    locked. Mirrors ``_handle_failure``'s terminal branch: set
    failed_state, mark completed.

    If the transition can't be restored (model uninstalled / transition
    renamed), we still mark_as_completed so the retry loop stops; the
    failed_state write is skipped — there's nothing to call it on.

    Returns the ``(transition, state, kwargs, exception)`` tuple the caller
    needs to run ``failure_callbacks`` *after* its atomic block commits
    (so callbacks don't run while holding the row lock, matching the
    in-task hook timing), or ``None`` when the row was unrestorable
    (nothing to run callbacks on).
    """
    try:
        # Savepointed: _restore queries (the instance, and restore_user hits
        # the user table), so a genuine DatabaseError inside it would poison
        # the caller's atomic and take the completion below down with it.
        with transaction.atomic():
            _, process, transition = _restore(transition_message)
    except _RestoreError:
        # No attempt ran here, so started_at (if any) belongs to an
        # abandoned attempt — don't record a misleading duration.
        transition_message.mark_as_completed(measure_duration=False)
        return None
    except Exception as exc:
        # Anything _restore did not classify as permanent (a consumer
        # ``process`` property raising, a corrupt instance_id, a DB blip).
        # Escaping here rolled back the whole finalization on every tick, so
        # the safety net looped forever on this one row. Completing it stops
        # the loop; the instance stays in its in_progress_state, which is an
        # implicit source — an operator can run the transition again.
        transition_logger.error(
            f'{source}: TransitionMessage#{transition_message.pk} could not be restored '
            f'({type(exc).__name__}: {exc}); completing it so the safety net '
            f'stops retrying. The instance is left parked in its '
            f'in_progress_state, which is an implicit source of the same '
            f'transition — run it again to move it on.',
            exc_info=True,
        )
        transition_message.mark_as_completed(measure_duration=False)
        return None

    try:
        with transaction.atomic():
            kwargs = deserialize_kwargs(transition_message.kwargs)
    except Exception as exc:
        # Terminal finalization must not be blocked by kwargs that no
        # longer decode: proceed with empty kwargs so failed_state and
        # completion still land and the retry loop stops. The savepoint
        # keeps the outer transaction healthy if the failure was a
        # genuine DatabaseError (restore_user queries the user table).
        transition_logger.error(
            f'{source}: TransitionMessage#{transition_message.pk} kwargs failed to decode '
            f'({type(exc).__name__}: {exc}); finalizing with empty kwargs.'
        )
        kwargs = {}
    # Mirror the sync path: side-effects/callbacks may read ``context``.
    kwargs.setdefault('context', {})
    state = process.state

    # Same state guard as the worker attempt path, and with the same verdict:
    # a manual ops fix wins WHOLE (CLAUDE.md contract 7), not just against the
    # failed_state write. Guarding only the write meant a safety net still ran
    # failure hooks against an instance an operator had already resolved —
    # report-back callbacks for a child that was fixed by hand — and
    # completed the row with no ``[superseded]`` marker to explain it.
    matches, expected, current = _state_guard_matches(transition, state)
    if not matches:
        note = (
            f'[superseded] {source} state guard: expected {expected}, found '
            f'{current!r} — the instance was moved by something else while '
            f'this row was pending. failed_state and failure hooks skipped; '
            f'the external state change wins. Prior error: '
            f'{transition_message.last_error_message or "(none recorded)"}'
        )
        transition_logger.error(
            f'{source}: TransitionMessage#{transition_message.pk} {transition.action_name} '
            f'{state.instance_key}: {note}'
        )
        transition_message.mark_as_superseded(note)
        return None

    if transition.failed_state:
        # Savepointed and never allowed to escape: this runs inside the
        # caller's atomic with the row locked, so a rejected write used to
        # abort the whole finalization — leaving the row uncompleted with
        # errors_count already at MAX_ERRORS, which detect_stuck retried on
        # every tick forever. Completing the row is the thing that stops the
        # loop. Opened on the instance's alias (set_state routes there) and
        # through _run_in_savepoint, so a rollback releases any deferred
        # unlock registered inside it instead of leaking it until TTL.
        previous = state.get_state()
        try:
            # require_commit: the else-branch below logs the write as landed,
            # so a silently discarded savepoint must surface as the failure
            # it is and take the honest except-path instead.
            _run_in_savepoint(
                state.instance._state.db or DEFAULT_DB_ALIAS,
                lambda: state.set_state(transition.failed_state),
                require_commit=True,
            )
        except Exception as write_error:
            # Restore the attribute a discarded savepoint left refreshed —
            # the failure hooks below must not see a state the database
            # never had.
            setattr(state.instance, state.field_name, previous)
            transition_logger.error(
                f'{source}: could not write failed_state='
                f'{transition.failed_state!r} on {state.instance_key}: '
                f'{type(write_error).__name__}: {write_error}. '
                f'Completing the row anyway so it stops retrying.',
                exc_info=True,
            )
            transition_message.record_failure_side_effect_error(
                write_error, label='failed_state write')
        else:
            transition_logger.info(
                f'{source}: set failed_state={transition.failed_state} '
                f'on {state.instance_key}'
            )

    # A safety-net finalization is not a worker attempt; started_at points
    # at the abandoned attempt, so don't let it inflate duration_ms.
    transition_message.mark_as_completed(measure_duration=False)
    return (transition, state, kwargs, exception)


def _run_failure_callbacks(transition, state, kwargs, exception) -> None:
    """Run a terminal row's ``failure_callbacks`` best-effort, *after* the
    finalizing atomic block has committed and released the row lock.

    The single implementation for every terminal-failure path: a row that hit
    MAX_ERRORS in-task, and one finalized by the watchdog / detect_stuck
    tasks. (There used to be a second copy taking an ``_Outcome`` instead of
    these four values, with a byte-identical body and log line.)
    ``Callbacks.execute`` already swallows exceptions; the guard here is
    belt-and-suspenders against a malformed hook list.
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
    # Invariant: everything that must survive together lives inside this
    # atomic block — row lock, side-effects, the target state write, and
    # either mark_as_completed (on success / terminal failure) or the
    # errors_count increment (on retryable failure). Moving any of the
    # mark_as_* / record_error calls out is what broke the unrestorable-row
    # path (see _StopRetry). Don't do it.
    #
    # started_at is the deliberate exception: it is a MARKER, not
    # accounting, and it has to be visible to other connections while the
    # attempt runs and to survive the attempt rolling back — so it is stamped
    # and committed by the caller before this block opens.
    with transaction.atomic():
        try:
            transition_message = (
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

        # Per-transition monitoring identity (Sentry transaction name + tags);
        # best-effort, no-op without sentry-sdk. See observability.py.
        set_sentry_context(transition_message)

        # A decode failure must be accounted like any attempt failure —
        # raised here it would escape before record_error with errors_count
        # still 0, and retry_stale_transitions would re-dispatch the row
        # forever. Hold the error and route it through _handle_failure once
        # the row is restored below. The savepoint keeps the outer
        # transaction healthy if the failure was a genuine DatabaseError
        # (restore_user queries the user table), so the error bookkeeping
        # always works.
        decode_error = None
        try:
            with transaction.atomic():
                kwargs = deserialize_kwargs(transition_message.kwargs)
        except Exception as exc:
            decode_error = exc
            kwargs = {}
        # Mirror the synchronous path (Transition._init_transition_context):
        # side-effects/callbacks may read a framework-provided ``context``
        # dict. serialize_kwargs drops it at enqueue, so rebuild it here —
        # otherwise a side-effect declared as ``def fn(instance, context,
        # **kwargs)`` works synchronously but raises in background mode.
        kwargs.setdefault('context', {})

        restore_error = None
        try:
            # Savepointed for the same reason as the decode above: _restore
            # queries the instance and (for user kwargs) the user table, so a
            # genuine DatabaseError must poison only the savepoint or the
            # error bookkeeping below cannot run.
            with transaction.atomic():
                instance, process, transition = _restore(transition_message)
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
            # _restore only classifies the PERMANENT failures (model
            # uninstalled, row gone, transition renamed) as _RestoreError.
            # Anything else — a consumer ``process`` property raising, a
            # corrupt instance_id failing pk coercion, a transient database
            # error — used to escape the worker with errors_count still 0, so the
            # starter re-dispatched the row forever: the same unaccounted
            # infinite-retry class that rejected state writes used to have.
            # Account it like any other attempt failure instead: transient
            # causes get their retries, permanent ones burn MAX_ERRORS and stop.
            restore_error = exc

        if restore_error is not None:
            return _handle_restore_failure(transition_message, restore_error)

        state = process.state

        # State guard: the worker restores by name and deliberately bypasses
        # the source-state gate, so without this check it would overwrite
        # any state change made while the row was pending — including a
        # manual ops fix. With retries spanning RETRY_MINUTES × MAX_ERRORS
        # that collision is a realistic production event.
        matches, expected, current = _state_guard_matches(transition, state)
        if not matches:
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

        # started_at was already stamped and committed by the caller
        # (stamp_attempt_started), so the row loaded above carries it and
        # the watchdog can see this attempt while it runs. Nothing to write
        # here — writing it inside this atomic is exactly what made the
        # stamp invisible.
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
                f'queue={transition_message.queue_name}'
            )
            if decode_error is not None:
                return _handle_failure(
                    transition_message, transition, state, kwargs, decode_error
                )
            def _attempt():
                for command in transition.side_effects.commands:
                    transition_logger.info(
                        f'{kwargs.get("tr_id")} '
                        f'{TransitionEventType.SIDE_EFFECT.value} '
                        f'{getattr(command, "__name__", repr(command))}'
                    )
                    command(instance, **kwargs)
                # The target write belongs INSIDE the attempt savepoint.
                # It is part of the attempt, so a write the
                # database rejects — CHECK constraint, pre_save receiver,
                # save() override, column length — must roll the attempt
                # back and be accounted like any other failure. Outside
                # it, the exception escaped the outer atomic and took
                # record_error with it: errors_count stayed 0, so the row
                # was re-dispatched forever, its side-effects re-ran
                # forever, and no safety net could terminate it.
                # Keeping it here also preserves the all-or-nothing
                # per-attempt contract the savepoint promises.
                if not isinstance(transition, BackgroundAction):
                    state.set_state(transition.target)
                    transition_logger.info(
                        f'{kwargs.get("tr_id")} '
                        f'{TransitionEventType.SET_STATE.value} '
                        f'{transition.target}'
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
                #
                # Through _run_in_savepoint, and on the INSTANCE's alias:
                # side-effects are consumer code that may drive synchronous
                # transitions on OTHER instances, whose DEFER_UNLOCK unlocks
                # ride transaction.on_commit. A rollback here discards those
                # hooks (Django drops them with the savepoint) while the outer
                # transaction still commits the bookkeeping — leaking a lock on
                # an instance whose state write was rolled back, until its TTL.
                # Every hook bundle already routes through this helper; the
                # attempt savepoint was the one raw atomic left.
                #
                # require_commit, because _handle_success below records the
                # work as done: a side-effect that raises a database error and
                # suppresses it (`try: obj.save() except IntegrityError: pass`
                # without a nested atomic) makes Django discard the savepoint
                # with nothing propagating. The attempt then "returns
                # successfully" having committed none of its writes — a
                # completed row, success callbacks and next_transition on top
                # of work that was thrown away. Accounted as a failure
                # instead, which is what it is.
                _run_in_savepoint(
                    instance._state.db or DEFAULT_DB_ALIAS, _attempt,
                    require_commit=True,
                )
            except Exception as error:
                return _handle_failure(transition_message, transition, state, kwargs, error)
            else:
                return _handle_success(transition_message, transition, state, kwargs)
        finally:
            _transition_context.reset(token)


def _handle_restore_failure(
    transition_message: TransitionMessage, error: BaseException,
) -> _Outcome:
    """Account a restore failure that is not one of the permanent classes.

    There is no transition object to fail through, so this is deliberately
    thinner than ``_handle_failure``: record the error, retry while retries
    remain, and at ``MAX_ERRORS`` complete the row loudly so the retry loop
    stops. No ``failed_state`` is written (nothing restored to write it on),
    so the instance is left parked in its ``in_progress_state`` — which is an
    implicit source of the same transition, so it can be run again: a visible
    parked state rather than an infinite loop, with the reason on the completed
    row.
    """
    transition_message.record_error(error)
    transition_logger.error(
        f'TransitionMessage#{transition_message.pk} restore raised '
        f'{type(error).__name__}: {error}',
        exc_info=True,
    )
    if transition_message.errors_count < bg_settings.max_errors():
        return _Outcome(terminal=False, succeeded=False, exception=error)

    transition_logger.error(
        f'TransitionMessage#{transition_message.pk} restore failed {transition_message.errors_count} times; '
        f'completing the row so it stops retrying. No failed_state could be '
        f'written — the instance is left parked in its in_progress_state, '
        f'ready to run again via the implicit source.'
    )
    transition_message.mark_as_completed(measure_duration=False)
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

    max_errors = bg_settings.max_errors()
    if transition_message.errors_count < max_errors:
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
    #
    # Savepointed, and never allowed to escape. This write comes
    # AFTER record_error, so letting it propagate rolled that error back and
    # pinned errors_count one below MAX_ERRORS forever — the row could never
    # terminalise and retried indefinitely. Completing the row is what stops
    # the retry loop, so a rejected failed_state must not prevent it: log it,
    # record it where an operator will see it, and carry on to
    # mark_as_completed. The instance is then left in its in_progress_state
    # and can be run again via the implicit source — a visible parked
    # state rather than an infinite loop.
    if transition.failed_state:
        previous = state.get_state()
        try:
            # On the instance's alias, and via _run_in_savepoint: see the
            # attempt savepoint in _run_atomic for why both matter.
            # require_commit: the else-branch below logs SET_STATE, so a
            # silently discarded savepoint must surface as the failure it is
            # and take the honest except-path instead.
            _run_in_savepoint(
                state.instance._state.db or DEFAULT_DB_ALIAS,
                lambda: state.set_state(transition.failed_state),
                require_commit=True,
            )
        except Exception as write_error:
            # Restore the attribute a discarded savepoint left refreshed —
            # the failure hooks below must not see a state the database
            # never had.
            setattr(state.instance, state.field_name, previous)
            transition_logger.error(
                f'{kwargs.get("tr_id")} could not write failed_state '
                f'{transition.failed_state!r} on {state.instance_key}: '
                f'{type(write_error).__name__}: {write_error}. Completing the '
                f'row anyway so it stops retrying; the instance stays parked '
                f'in {transition.in_progress_state!r}, ready to run again '
                f'via the implicit source.',
                exc_info=True,
            )
            transition_message.record_failure_side_effect_error(
                write_error, label='failed_state write')
        else:
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
                f'{transition.failed_state}'
            )
    transition_message.mark_as_completed()
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
    try:
        app = apps.get_app_config(transition_message.app_label)
        model = app.get_model(transition_message.model_name)
    except LookupError as exc:
        raise _RestoreError(
            f'model {transition_message.app_label}.{transition_message.model_name} not installed'
        ) from exc

    try:
        # _base_manager, not objects: a filtered default manager (e.g. one
        # that hides archived/soft-deleted rows) would raise DoesNotExist
        # for an instance that still exists, and the restore-error path
        # would mark the message completed — stranding the instance in
        # in_progress_state with no failed_state and no retries. Framework
        # code reloading by pk must be immune to default-manager filtering
        # (Django's own convention for related-object loading).
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
        try:
            process = _load_process_from_path(instance, recorded_path, transition_message)
        except Exception as exc:
            # Fail closed, through the accounted stop-retry path. A raw
            # ImportError here would escape _run_atomic (which only
            # catches _RestoreError), roll the attempt back with
            # errors_count still untouched, and retry_stale_transitions
            # would re-dispatch the row forever.
            raise _RestoreError(
                f'recorded process_class {recorded_path!r} could not be '
                f'loaded: {exc}'
            ) from exc
    else:
        # Verify the attribute resolved the same class enqueue recorded.
        # Every Process defaults to process_name='process', so a name
        # collision (directly-instantiated process vs the bound one, or a
        # rebind between enqueue and execute) silently restores the WRONG
        # class — the worker would run side-effects the caller never asked
        # for. Prefer the recorded class on mismatch.
        if recorded_path:
            resolved_path = f'{type(process).__module__}.{type(process).__name__}'
            if resolved_path != recorded_path:
                transition_logger.warning(
                    f'TransitionMessage#{transition_message.pk}: process_name '
                    f'{transition_message.process_name!r} resolved to {resolved_path}, but '
                    f'the message was enqueued by {recorded_path}; using '
                    f'the recorded class.'
                )
                try:
                    process = _load_process_from_path(
                        instance, recorded_path, transition_message
                    )
                except Exception as exc:
                    # Fail closed: running the attribute-resolved process
                    # instead would execute side-effects enqueue never
                    # asked for. The row completes as unrestorable (no
                    # side-effects, no state write): complete pending
                    # rows before renaming a Process class.
                    raise _RestoreError(
                        f'recorded process_class {recorded_path!r} could '
                        f'not be loaded: {exc}'
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
    """Resolve the exact background transition a ``TransitionMessage`` refers to.

    Enqueue can record a background transition declared on a *nested*
    process (the sync lookup recurses into ``nested_processes``), but the
    message records only the *bound* ``process_name``, so the worker
    restores the parent and must descend the ``nested_processes`` tree —
    each sub-process constructed with the parent's shared ``state``,
    exactly the way ``Process.get_available_transitions`` does. Without
    this descent the nested transition is never found: the message is
    marked completed, the side-effects never run, and the instance is
    stranded in ``in_progress_state``.

    Enqueue also records the (possibly nested) process class that
    declared the transition on ``transition_message.owning_process_class``.
    When present it pins the search to that exact class, so an
    ``action_name`` shared across nested processes that use conditions
    to choose resolves to the one enqueue actually chose (see
    ``_validate_unique_background_action_names``). It is recorded for
    *every* background transition started through the Process
    entrypoint — for a transition on the bound process itself it equals
    the bound class. It is blank only for rows enqueued before this
    column existed (pre-0.4.x) or, rarely, outside the Process
    entrypoint.

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

    Only ``is_background`` transitions are candidates: the worker never
    restores a synchronous transition (a ``TransitionMessage`` is created
    solely by enqueue). A state-aware lookup would not work here either
    — the worker runs while the instance sits in ``in_progress_state``
    (not in the transition's declared ``sources``), and the sync path's
    lookup is gated on state membership; we bypass that gate
    deliberately.
    """
    owning_path = (transition_message.owning_process_class or '').strip()
    if owning_path:
        found = _find_background_transition_in_owner(
            process, transition_message.transition_name, owning_path
        )
        if found is not None:
            return found
        # The owner was recorded but is not in the tree — e.g. the nested
        # process class was renamed/removed between enqueue and execute.
        # Fall through to the name-based fallback (which refuses to guess
        # if the name is ambiguous), logging so the mismatch is visible.
        transition_logger.warning(
            f'TransitionMessage#{transition_message.pk}: recorded process class '
            f'{owning_path!r} for background transition '
            f'{transition_message.transition_name!r} was not found in the process tree '
            f'(renamed or removed?); attempting name-based fallback.'
        )

    matches = _background_transitions_named(process, transition_message.transition_name)
    if len(matches) == 1:
        # Unambiguous — the legacy/pre-discriminator common case, safe to use.
        return matches[0]
    if len(matches) > 1:
        # Ambiguous AND no resolvable owner: do NOT guess. Raising _RestoreError
        # finalizes the row (stops retries) without running any side-effects —
        # far safer than running the wrong condition-disambiguated sibling.
        raise _RestoreError(
            f'background transition {transition_message.transition_name!r} matches '
            f'{len(matches)} transitions across the process tree and the '
            f'message has no resolvable owning_process_class '
            f'(recorded={transition_message.owning_process_class!r}); refusing to guess which '
            f'condition-disambiguated sibling to run. This is an uncompleted '
            f'row enqueued before the owner column existed, or whose nested '
            f'process class was renamed or removed while the row was pending. '
            f'Complete pending rows before refactoring a background '
            f'action_name into shared nested processes.'
        )
    return None  # zero matches -> generic not-found _RestoreError in _restore


def _find_background_transition_in_owner(process, action_name, owning_path):
    """Return the background transition named ``action_name`` declared on the
    process in the tree whose dotted class path equals ``owning_path``.

    Walks through ``_iter_process_tree`` for its cycle guard: a nested topology
    that revisits a class (A nests B nests A — legal, and blessed by the sync
    walk) recursed until ``RecursionError`` here, on the very
    fall-through path the caller is written to handle gracefully.
    """
    for process_cls in _iter_process_tree(type(process)):
        proc_path = f'{process_cls.__module__}.{process_cls.__name__}'
        if proc_path != owning_path:
            continue
        for transition in process_cls.transitions:
            if (
                transition.action_name == action_name
                and getattr(transition, 'is_background', False)
            ):
                return transition
        # Class matched but it no longer declares the transition (renamed).
        return None
    return None


def _background_transitions_named(process, action_name):
    """All distinct ``is_background`` transitions named ``action_name`` across the
    process and its nested tree.

    De-duplicated by transition identity so a Process class legitimately reached
    via two nested paths (its class-level ``transitions`` are shared objects)
    counts once — otherwise the ambiguity check in ``_find_transition`` would
    false-positive on a reused sub-process. ``_iter_process_tree`` supplies the
    class-level cycle guard (see ``_find_background_transition_in_owner``).
    """
    seen, out = set(), []
    for process_cls in _iter_process_tree(type(process)):
        for transition in process_cls.transitions:
            if (
                transition.action_name == action_name
                and getattr(transition, 'is_background', False)
                and id(transition) not in seen
            ):
                seen.add(id(transition))
                out.append(transition)
    return out
