"""Pull execution: workers claim rows from the database.

The committed ``TransitionMessage`` row is the signal. A worker asks the
database for one claimable row, runs the shared execute path on it, and
asks again. Nothing is sent to a broker, so nothing can be lost,
duplicated, or published to a queue nobody consumes — the defect class
recorded in the design record (``docs/design/PULL_WORKERS.md``, issue
#217).

The claim's WHERE clause is the retry rule:

* a fresh row is claimable at once;
* a row whose attempt just failed becomes claimable again after
  ``RETRY_MINUTES`` — nothing has to re-dispatch it, it is simply
  visible again;
* a row whose attempt runs right now is row-locked by that attempt, so
  ``SKIP LOCKED`` passes over it;
* a worker that dies releases its lock with its connection, so its row
  is claimable at once.

``LISTEN/NOTIFY`` is the wake-up: enqueue fires one payload-free
notification after commit, and a waiting worker asks the database at
once instead of at the next poll. A lost notification costs one poll
interval — the row waits in the database either way.

The worker loop also runs the safety nets (the stuck finalizer and its
no-worker report, the cleanup sweep), so nothing has to be scheduled
anywhere else.
"""
from __future__ import annotations

import os
import select
import signal
import time

from django.db import DEFAULT_DB_ALIAS, connections, router, transaction

from django_logic.logger import logger

#: One channel for every queue. The notification carries no payload and
#: means only "ask the database now"; the claim's queue filter does the
#: routing, so per-queue channels would buy nothing.
NOTIFY_CHANNEL = 'django_logic_work'

#: The floor under LISTEN/NOTIFY: a worker asks the database at least
#: this often even when no notification arrives.
POLL_SECONDS = 5.0

#: How often the loop runs the safety nets (stuck report, cleanup).
SAFETY_NET_SECONDS = 60.0


def notify_workers() -> None:
    """Tell every listening worker to ask the database now. Best effort:
    a lost notification is covered by the poll floor."""
    from django_logic.background.models import TransitionMessage

    alias = router.db_for_write(TransitionMessage) or DEFAULT_DB_ALIAS
    try:
        with connections[alias].cursor() as cursor:
            cursor.execute(f'NOTIFY {NOTIFY_CHANNEL}')
    except Exception as exc:
        logger.warning('pull: NOTIFY failed (the poll floor covers it): %s', exc)


def claim_next(queues: list[str]) -> int | None:
    """Return the pk of one claimable row for ``queues``, or ``None``.

    The lock taken by ``SKIP LOCKED`` is released when this short
    transaction ends; the runner then takes its own row lock for the
    attempt. Two workers can race through that gap, and the loser exits
    through the runner's existing skip-if-locked guard — wasteful once
    in a while, never wrong.
    """
    from django_logic.background.safety_nets import _claimable

    with transaction.atomic():
        return (
            _claimable(queues)
            .select_for_update(skip_locked=True)
            .values_list('pk', flat=True)
            .first()
        )


def run_once(queues: list[str], *, isolate: bool) -> bool:
    """Claim and execute at most one row. Returns whether one ran.

    With ``isolate=True`` (what the worker loop uses) the attempt runs in
    a forked child process, so a crash — ``os._exit`` in consumer code, a
    segmentation fault, the platform's memory killer — kills the attempt,
    not the worker. The parent records the death as an error on the row,
    which gives a crashing attempt the same paced, bounded retries as a
    failing one; before this, every crash killed the whole worker process
    and the platform's restart backoff parked the queue group with it.
    """
    from django_logic.background.runner import run_background_transition

    pk = claim_next(queues)
    if pk is None:
        return False
    if isolate and hasattr(os, 'fork'):
        _run_attempt_in_child(pk)
    else:
        run_background_transition(pk)
    return True


def _run_attempt_in_child(pk: int) -> None:
    """Run one attempt in a forked child, bound it, and account for its death.

    Both sides must not share a database connection — a connection closed
    (or crashed) on one side poisons the other's session. The parent
    closes every connection before the fork; each side then opens its
    own lazily.

    The parent enforces the transition's declared ``timeout=``: when the
    child runs past the budget, the parent kills it. This is the only
    place a hanging attempt can be stopped — the attempt holds its row
    lock while it runs, so nothing else can reach it.

    A child that dies without completing the row left no error on it, so
    the parent records one: the claim's retry wait then paces the next
    attempt, and ``MAX_ERRORS`` bounds a crash loop — a side-effect that
    crashes (or hangs) every time ends in ``failed_state`` like one that
    fails every time, instead of looping forever.
    """
    from django.db import connections

    from django_logic.background.models import TransitionMessage
    from django_logic.background.runner import run_background_transition

    timeout_seconds = (
        TransitionMessage.objects
        .filter(pk=pk)
        .values_list('timeout_seconds', flat=True)
        .first()
    )
    connections.close_all()
    child = os.fork()
    if child == 0:
        status = 1
        try:
            run_background_transition(pk)
            status = 0
        finally:
            # _exit, so a crashing attempt cannot run the parent's cleanup
            # handlers or flush its buffers twice.
            os._exit(status)
    exit_code, timed_out = _wait_for_child(child, timeout_seconds)
    if timed_out:
        logger.error(
            f'pull: the attempt for TransitionMessage#{pk} ran past its '
            f'declared timeout={timeout_seconds}s and was stopped. The error '
            f'recorded here paces the next claim.'
        )
        _record_child_death(
            pk,
            f'[timeout] the attempt ran past timeout={timeout_seconds}s '
            f'and was stopped',
        )
        return
    if exit_code == 0:
        return
    logger.error(
        f'pull: the attempt process for TransitionMessage#{pk} died '
        f'(exit {exit_code}). Its row lock died with it; the error recorded '
        f'here paces the next claim.'
    )
    _record_child_death(
        pk,
        f'[crashed] the attempt process died (exit {exit_code}) '
        f'before the attempt finished',
    )


def _wait_for_child(child: int, timeout_seconds) -> tuple[int, bool]:
    """Reap the child. Returns ``(exit_code, timed_out)``.

    With no declared budget the wait is plain and unbounded. With one,
    the parent polls the child and kills it (``SIGKILL`` — the attempt
    may hang inside code that ignores gentler signals) when the budget
    passes. The kill releases the child's row lock with its connection.
    """
    if timeout_seconds is None:
        _, raw_status = os.waitpid(child, 0)
        return os.waitstatus_to_exitcode(raw_status), False
    deadline = time.monotonic() + timeout_seconds
    while True:
        pid, raw_status = os.waitpid(child, os.WNOHANG)
        if pid != 0:
            return os.waitstatus_to_exitcode(raw_status), False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
            return -signal.SIGKILL, True
        time.sleep(min(1.0, max(0.01, remaining)))


def _record_child_death(pk: int, message: str) -> None:
    """Record a died attempt on the row — unless the row completed first.

    Another worker on the same queue can claim the row the moment the
    child's lock dies and finish it before this write. One conditional
    UPDATE keeps the guard and the write in the same statement, so a
    completed row can never take the death as an error.
    """
    from django.db.models import F
    from django.utils import timezone

    from django_logic.background.models import TransitionMessage, db_safe_text

    now = timezone.now()
    updated = TransitionMessage.objects.filter(
        pk=pk, is_completed=False,
    ).update(
        errors_count=F('errors_count') + 1,
        last_error_message=db_safe_text(message),
        last_error_dt=now,
        modified=now,
    )
    if not updated:
        logger.info(
            f'pull: TransitionMessage#{pk} completed on another worker '
            f'before the death could be recorded; nothing to record.'
        )


def _run_safety_nets() -> None:
    """The periodic work: the stuck finalizer and its never-started
    report, and the cleanup sweep. Called from the loop, so pull mode
    schedules nothing anywhere else."""
    from django_logic.background.safety_nets import (
        cleanup_completed_transitions,
        detect_stuck_transitions,
    )

    for step in (
        detect_stuck_transitions,
        cleanup_completed_transitions,
    ):
        try:
            step()
        except Exception as exc:
            logger.error('pull: safety net %s failed: %s',
                         getattr(step, '__name__', step), exc)


def _wait_for_work(timeout: float) -> None:
    """Sleep until a notification arrives or ``timeout`` passes.

    Holds one LISTEN connection per worker process. ``LISTEN`` lasts for
    the session, so it is issued once per connection, not per wait.
    psycopg 2 and 3 expose notifications differently, so the wait
    branches on the driver: psycopg 2 keeps a ``notifies`` list the
    caller drains after ``select``; psycopg 3 waits inside the
    ``notifies()`` generator. When the connection cannot listen (a
    pooler that rejects LISTEN, a broken socket), the wait degrades to
    a plain sleep and the poll floor carries the loop.
    """
    from django_logic.background.models import TransitionMessage

    alias = router.db_for_write(TransitionMessage) or DEFAULT_DB_ALIAS
    try:
        connection = connections[alias]
        connection.ensure_connection()
        raw = connection.connection
        if not getattr(raw, '_django_logic_listening', False):
            with raw.cursor() as cursor:
                cursor.execute(f'LISTEN {NOTIFY_CHANNEL}')
            raw._django_logic_listening = True
        if hasattr(raw, 'poll'):
            # psycopg 2. A notification that arrived during earlier
            # statements already sits in the list, so check it before
            # sleeping on the socket.
            raw.poll()
            if raw.notifies:
                del raw.notifies[:]
                return
            select.select([raw], [], [], timeout)
            raw.poll()
            del raw.notifies[:]
        else:
            # psycopg 3 (3.2 or later). The generator returns at the
            # first notification or when the timeout passes, and it
            # consumes what it yields, so nothing accumulates.
            for _ in raw.notifies(timeout=timeout, stop_after=1):
                pass
    except Exception as exc:
        logger.warning(
            'pull: the notification wait failed (%s); sleeping the poll '
            'interval instead.', exc,
        )
        time.sleep(timeout)


def run_worker(queues: list[str], *, forever: bool = True) -> None:
    """The worker loop: drain claimable rows, run the safety nets on
    schedule, wait for a notification, repeat.

    ``forever=False`` runs exactly one drain-and-safety-net pass — for
    tests and for a one-off catch-up command.
    """
    logger.info('pull worker starting: queues=%s', ','.join(queues))
    last_safety_net = 0.0
    while True:
        ran_any = False
        while run_once(queues, isolate=True):
            ran_any = True
            # A sustained backlog must not starve the safety nets: break
            # out of the drain when they are due and come back after.
            if time.monotonic() - last_safety_net >= SAFETY_NET_SECONDS:
                break
        if time.monotonic() - last_safety_net >= SAFETY_NET_SECONDS:
            _run_safety_nets()
            last_safety_net = time.monotonic()
        if ran_any:
            continue
        if not forever:
            return
        _wait_for_work(POLL_SECONDS)
