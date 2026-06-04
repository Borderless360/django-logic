"""Celery task wrappers + periodic safety-net tasks.

This module is import-safe even when Celery is not installed — the
``@shared_task`` decorator is a thin shim that, when Celery is absent,
degrades to a plain function with a ``.apply_async`` method that
executes inline. This is how Sync mode stays Celery-free.

Tasks defined here:

* :func:`run_background_transition_task` — executes phase 2 for one
  ``TransitionMessage``.
* :func:`retry_stale_transitions` — periodic; re-dispatches uncompleted
  messages back to their own queue.
* :func:`cleanup_completed_transitions` — periodic; deletes old
  completed messages.
* :func:`detect_stuck_transitions` — periodic; finalizes messages
  stuck at ``MAX_ERRORS`` (writes ``failed_state``, runs
  ``failure_side_effects``, marks completed) so the retry loop stops.
* :func:`watchdog_stale_attempts` — periodic; abandons phase-2
  attempts whose current run has exceeded their declared
  ``timeout_seconds``.

All five are registered under the ``django_logic`` namespace.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Min, Q
from django.utils import timezone

from django_logic.background import settings as bg_settings
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import (
    abandon_timed_out_attempt,
    finalize_stuck_attempt,
    run_background_transition,
)
from django_logic.logger import logger


try:
    from celery import shared_task as _celery_shared_task
    _CELERY_AVAILABLE = True
except ImportError:
    _CELERY_AVAILABLE = False

    def _celery_shared_task(*task_args, **task_kwargs):
        """Drop-in stand-in for ``celery.shared_task`` when Celery isn't installed.

        The returned object behaves like a function but also exposes
        ``.apply_async`` and ``.delay``. Both run the task inline — in
        Sync mode, that's exactly what we want; in Celery mode, this
        path is not reachable because ``validate_on_ready`` would have
        raised.
        """
        def decorator(func):
            class _InlineTask:
                name = f'{func.__module__}.{func.__name__}'

                def __call__(self, *args, **kwargs):
                    return func(*args, **kwargs)

                def apply_async(self, args=None, kwargs=None, queue=None, **_ignored):
                    return func(*(args or ()), **(kwargs or {}))

                def delay(self, *args, **kwargs):
                    return func(*args, **kwargs)

            return _InlineTask()

        # ``@shared_task`` can be used bare or with args; handle both.
        if task_args and callable(task_args[0]) and not task_kwargs:
            return decorator(task_args[0])
        return decorator


@_celery_shared_task(
    acks_late=True,
    name='django_logic.run_background_transition',
    bind=False,
)
def run_background_transition_task(transition_message_id: int) -> None:
    """Phase-2 entrypoint for one transition.

    Exceptions are re-raised so Celery's own retry / alerting machinery
    can react. The periodic starter is the primary retry path though;
    Celery-level retries are not configured here by design.
    """
    run_background_transition(transition_message_id)


@_celery_shared_task(
    acks_late=True,
    name='django_logic.retry_stale_transitions',
    bind=False,
)
def retry_stale_transitions() -> int:
    """Periodic: re-dispatch uncompleted messages older than ``RETRY_MINUTES``.

    Each message is dispatched back to its own ``queue_name`` — a slow
    export never ends up on the critical queue.

    Returns the number of messages re-dispatched.
    """
    return _retry_pending_inline()


def _retry_pending_inline() -> int:
    # Mirror dispatch_transition's mode awareness: in Sync mode there is
    # no Celery worker to consume an apply_async message (with no broker
    # configured Celery silently publishes to an in-memory transport that
    # nobody drains), so phase 2 must run inline. In Celery mode we
    # re-dispatch to the row's own queue. The check also honours an
    # active sync_execution() block.
    from django_logic.background.dispatch import _current_mode

    sync_mode = _current_mode() == bg_settings.EXECUTION_SYNC

    cutoff = timezone.now() - timedelta(minutes=bg_settings.retry_minutes())
    max_errors = bg_settings.max_errors()

    # Materialise the candidate rows up front rather than streaming with
    # iterator(): in Sync mode each row opens its own atomic block with
    # select_for_update, and holding a server-side cursor open across
    # those nested transactions is fragile across backends.
    #
    # Recency guard: skip rows whose *current* attempt started within
    # RETRY_MINUTES. Without it, a row matches on created<cutoff every tick
    # and gets re-dispatched repeatedly while an attempt is still in flight
    # (the select_for_update guard prevents double-execution, but duplicate
    # queue messages pile up and the redispatch keeps overwriting
    # started_at, perpetually sliding the watchdog's timeout floor). Rows
    # that never started (started_at IS NULL) are always eligible.
    candidates = list(
        TransitionMessage.objects
        .filter(
            is_completed=False,
            errors_count__lt=max_errors,
            created__lt=cutoff,
        )
        .filter(Q(started_at__isnull=True) | Q(started_at__lt=cutoff))
        .order_by('created')
        .values_list('pk', 'queue_name')
    )

    dispatched = 0
    for pk, queue_name in candidates:
        try:
            if sync_mode:
                # Run the attempt inline. Side-effect failures re-raise
                # out of run_background_transition; we treat that like a
                # dispatch failure for this row and keep scanning.
                run_background_transition(pk)
            else:
                run_background_transition_task.apply_async(
                    args=[pk], queue=queue_name
                )
            dispatched += 1
        except Exception as e:
            # A dispatch-layer error (broker down, serialization, etc.)
            # or an inline phase-2 failure shouldn't stop us from trying
            # the remaining rows.
            logger.error(
                'retry_stale_transitions: failed to dispatch '
                f'TransitionMessage#{pk}: {e}'
            )
    if dispatched:
        logger.info(
            f'retry_stale_transitions: dispatched {dispatched} stale '
            f'TransitionMessage rows'
        )
    return dispatched


@_celery_shared_task(
    acks_late=True,
    name='django_logic.cleanup_completed_transitions',
    bind=False,
)
def cleanup_completed_transitions() -> int:
    """Periodic: delete completed messages older than ``CLEANUP_DAYS``."""
    cutoff = timezone.now() - timedelta(days=bg_settings.cleanup_days())
    with transaction.atomic():
        deleted, _ = (
            TransitionMessage.objects
            .filter(is_completed=True, modified__lt=cutoff)
            .delete()
        )
    if deleted:
        logger.info(f'cleanup_completed_transitions: deleted {deleted} rows')
    return deleted


@_celery_shared_task(
    acks_late=True,
    name='django_logic.detect_stuck_transitions',
    bind=False,
)
def detect_stuck_transitions() -> int:
    """Periodic: finalize messages stuck at ``MAX_ERRORS`` so they reach a
    terminal state (``failed_state`` if declared on the transition) and
    get out of the retry set.

    Previously this only logged; a row that hit MAX_ERRORS without going
    through the in-task terminal path (e.g. worker killed mid-atomic
    after ``record_error`` committed on a prior attempt) would sit
    uncompleted forever. Now each such row is forcibly terminated,
    with one ERROR log line per row.

    Rows currently being processed by a worker (row-locked) are skipped
    this tick — the running attempt will finalize them naturally.

    Returns the number of rows finalized.
    """
    max_errors = bg_settings.max_errors()
    stuck_ids = list(
        TransitionMessage.objects
        .filter(is_completed=False, errors_count__gte=max_errors)
        .values_list('pk', flat=True)
    )
    finalized = 0
    for tm_id in stuck_ids:
        try:
            if finalize_stuck_attempt(tm_id):
                finalized += 1
        except Exception as e:
            # One bad row shouldn't stop the scan.
            logger.error(
                f'detect_stuck_transitions: failed to finalize '
                f'TransitionMessage#{tm_id}: {e}'
            )
    return finalized


@_celery_shared_task(
    acks_late=True,
    name='django_logic.watchdog_stale_attempts',
    bind=False,
)
def watchdog_stale_attempts() -> int:
    """Periodic: abandon phase-2 attempts that have been running beyond
    their declared ``timeout_seconds``.

    Only rows that opted in via ``BackgroundTransition(timeout=N)`` are
    scanned. For each stale row we record a synthetic ``TimeoutError``
    so the retry machinery treats it as a failed attempt; when
    ``errors_count`` hits ``MAX_ERRORS`` the row is finalized with
    ``failed_state`` (if declared).

    Rows held by a running worker (``select_for_update(nowait)``) are
    skipped this tick — the live worker will finish or fail on its own.
    The watchdog is about abandoned attempts, not slow ones.

    Returns the number of rows touched.
    """
    return _watchdog_stale_attempts_inline()


def _watchdog_stale_attempts_inline() -> int:
    """Scan uncompleted timeout rows for stale attempts.

    The scan is narrowed by a DB-side ``started_at`` floor: we first
    compute ``Min(timeout_seconds)`` over in-flight timeout rows, then
    filter ``started_at < now - min_timeout``. That bound excludes every
    row whose attempt can't possibly be stale yet, regardless of its
    per-row timeout. The remaining per-row comparison runs in Python
    (portable across backends).

    At low volumes the floor is effectively free; at high volumes it
    keeps the working set bounded by "rows old enough for the fastest
    timeout to fire".
    """
    now = timezone.now()

    base = TransitionMessage.objects.filter(
        is_completed=False,
        started_at__isnull=False,
        timeout_seconds__isnull=False,
    )
    min_timeout = base.aggregate(m=Min('timeout_seconds'))['m']
    if min_timeout is None:
        return 0

    floor = now - timedelta(seconds=min_timeout)
    candidates = (
        base.filter(started_at__lt=floor)
        .values_list('pk', 'started_at', 'timeout_seconds')
    )

    touched = 0
    for pk, started_at, timeout_seconds in candidates:
        if started_at + timedelta(seconds=timeout_seconds) >= now:
            continue
        try:
            if abandon_timed_out_attempt(pk):
                touched += 1
        except Exception as e:
            logger.error(
                f'watchdog_stale_attempts: failed on '
                f'TransitionMessage#{pk}: {e}'
            )
    return touched
