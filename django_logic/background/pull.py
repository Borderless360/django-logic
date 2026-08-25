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
interval — the row waits in the database either way. The wake-up exists
for pickup latency on a direct Postgres connection; pgbouncer
transaction pooling rejects LISTEN, and there the worker falls back to
the poll floor.

Each attempt runs in a forked attempt process, so a crash kills the
attempt and not the worker. ``--concurrency`` says how many of them a
worker runs at a time; the default of one keeps the worker sequential.

The worker loop also runs the safety nets (the stuck finalizer and its
no-worker report, the cleanup sweep), so nothing has to be scheduled
anywhere else.
"""
from __future__ import annotations

import os
import select
import signal
import time
from dataclasses import dataclass

from django.db import DEFAULT_DB_ALIAS, connections, router, transaction

from django_logic.logger import logger

#: One channel for every queue. The notification carries no payload and
#: means only "ask the database now"; the claim's queue filter does the
#: routing, so per-queue channels would buy nothing.
NOTIFY_CHANNEL = 'django_logic_work'

#: The floor under LISTEN/NOTIFY: a worker asks the database at least
#: this often even when no notification arrives.
POLL_SECONDS = 5.0

#: The floor while the worker still has attempts running. Shorter than
#: ``POLL_SECONDS``: an attempt that crashes leaves no error on its row,
#: and only the worker can record one.
BUSY_POLL_SECONDS = 1.0

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


def claim_next(queues: list[str], *, exclude_pks=()) -> int | None:
    """Return the pk of one claimable row for ``queues``, or ``None``.

    The lock taken by ``SKIP LOCKED`` is released when this short
    transaction ends; the runner then takes its own row lock for the
    attempt. Two workers can race through that gap, and the loser exits
    through the runner's existing skip-if-locked guard — wasteful once
    in a while, never wrong.

    ``exclude_pks`` names the rows this worker already runs. One worker
    claims faster than its own attempt processes take their row locks, so
    without this it would claim the same head row into every free slot.
    """
    from django_logic.background.safety_nets import _claimable

    rows = _claimable(queues)
    if exclude_pks:
        rows = rows.exclude(pk__in=exclude_pks)
    with transaction.atomic():
        return (
            rows
            .select_for_update(skip_locked=True)
            .values_list('pk', flat=True)
            .first()
        )


def run_once(queues: list[str], *, isolate: bool) -> bool:
    """Claim and execute at most one row. Returns whether one ran.

    With ``isolate=True`` (what the worker loop uses) the attempt runs in
    a forked attempt process, so a crash — ``os._exit`` in consumer code, a
    segmentation fault, the platform's memory killer — kills the attempt,
    not the worker. The worker records the crash as an error on the row,
    so a crashing attempt gets the same paced, bounded retries as a
    failing one.
    """
    from django_logic.background.runner import run_background_transition

    pk = claim_next(queues)
    if pk is None:
        return False
    if isolate and hasattr(os, 'fork'):
        attempts: dict[int, _Attempt] = {}
        _start_attempt(pk, attempts)
        _harvest(attempts, block=True)
    else:
        run_background_transition(pk)
    return True


@dataclass
class _Attempt:
    """One forked attempt process the worker is responsible for.

    ``reaped`` means the process is gone and only the accounting write
    remains; ``exit_code`` is what the reap reported, or ``None`` when
    something else reaped it and no status arrived.
    """

    pk: int
    timeout_seconds: float | None
    deadline: float | None
    killed: bool = False
    reaped: bool = False
    exit_code: int | None = None


def _start_attempt(pk: int, attempts: dict[int, _Attempt]) -> None:
    """Fork one attempt process for ``pk`` and record it in ``attempts``.

    The worker and the attempt process must not share a database
    connection — a connection closed (or crashed) on one side poisons
    the other's session. The worker closes every connection before the
    fork; each side then opens its own lazily.
    """
    from django_logic.background.models import TransitionMessage
    from django_logic.background.runner import run_background_transition

    timeout_seconds = (
        TransitionMessage.objects
        .filter(pk=pk)
        .values_list('timeout_seconds', flat=True)
        .first()
    )
    connections.close_all()
    attempt_pid = os.fork()
    if attempt_pid == 0:
        # fork() answers 0 inside the attempt process itself.
        status = 1
        try:
            run_background_transition(pk)
            status = 0
        finally:
            # _exit, so a crashing attempt cannot run the worker's cleanup
            # handlers or flush its buffers twice.
            os._exit(status)
    attempts[attempt_pid] = _Attempt(
        pk=pk,
        timeout_seconds=timeout_seconds,
        deadline=(
            None if timeout_seconds is None
            else time.monotonic() + timeout_seconds
        ),
    )


def _harvest(attempts: dict[int, _Attempt], *, block: bool) -> None:
    """Account for the attempt processes that ended, and kill the ones
    that ran past their declared ``timeout=``.

    Killing is the only way to stop a hanging attempt: the attempt holds
    its row lock while it runs, so nothing else can reach it. The signal
    is ``SIGKILL`` because the attempt may hang inside code that ignores
    gentler signals, and the kill releases the row lock with the
    attempt's connection.

    With ``block`` the call returns after one attempt ends. It blocks in
    ``waitpid`` when no attempt carries a budget, and polls when one
    does, because a budget has to be enforced while nothing exits.

    An attempt leaves ``attempts`` only when its accounting write lands.
    The write can fail for the same reason the attempt crashed — a
    database outage — and losing it would give a crash loop unpaced,
    unbounded retries, so a failed write keeps the attempt as ``reaped``
    and every later pass retries it.
    """
    while attempts:
        now = time.monotonic()
        for pid in [p for p, a in attempts.items() if a.reaped]:
            if _try_account(attempts[pid]):
                del attempts[pid]
        if not attempts:
            return
        for pid, attempt in attempts.items():
            if attempt.reaped or attempt.killed or attempt.deadline is None:
                continue
            if now < attempt.deadline:
                continue
            attempt.killed = True
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                # It ended between the poll and the kill. The wait below
                # reaps it, and the budget passed either way, so the
                # attempt is still charged a timeout.
                pass
        if all(attempt.reaped for attempt in attempts.values()):
            # Only failed accounting writes remain — no process to wait
            # on. Pace the retry instead of hammering the database.
            if not block:
                return
            time.sleep(1.0)
            continue
        next_deadline = min(
            (
                attempt.deadline for attempt in attempts.values()
                if attempt.deadline is not None
                and not attempt.killed and not attempt.reaped
            ),
            default=None,
        )
        try:
            pid, raw_status = os.waitpid(
                -1, 0 if block and next_deadline is None else os.WNOHANG)
        except ChildProcessError:
            # Something else reaped them, so no exit status is coming.
            for attempt in attempts.values():
                if not attempt.reaped:
                    attempt.reaped = True
                    attempt.exit_code = None
                    attempt.deadline = None
            continue
        if pid == 0:
            if not block:
                return
            time.sleep(min(1.0, max(0.01, next_deadline - now)))
            continue
        attempt = attempts.get(pid)
        if attempt is None:
            # Not an attempt process. The worker is the only supervisor
            # here, so this is a stray child left by consumer code.
            continue
        attempt.reaped = True
        attempt.exit_code = os.waitstatus_to_exitcode(raw_status)
        attempt.deadline = None
        if _try_account(attempt):
            del attempts[pid]
        if block:
            return


def _try_account(attempt: _Attempt) -> bool:
    """Run the accounting write for a reaped attempt. Returns whether it
    landed.

    The write must not raise out of the worker loop: the database that
    refuses it is often the same one whose outage crashed the attempt,
    and a worker that dies here leaves its other attempts running with
    nothing to enforce their ``timeout=``.
    """
    try:
        _account(attempt, attempt.exit_code)
    except Exception as exc:
        logger.error(
            'pull: could not record how the attempt for '
            'TransitionMessage#%s ended (%s: %s); the worker keeps it and '
            'retries the write.',
            attempt.pk, type(exc).__name__, exc,
        )
        return False
    return True


def _account(attempt: _Attempt, exit_code: int | None) -> None:
    """Record one error on the row when the attempt did not end cleanly.
    ``exit_code`` is ``None`` when something else reaped the attempt, so
    no status reached the worker.

    An attempt process that dies without completing the row left no
    error on it, so the worker records one: the claim's retry wait then
    paces the next attempt, and ``MAX_ERRORS`` bounds a crash loop — a
    side-effect that crashes (or hangs) every time ends in
    ``failed_state`` like one that fails every time, instead of looping
    forever.

    An attempt the worker killed is a timeout even when its status never
    arrives. It is not a timeout when the status shows it ended on its
    own first: ``os.kill`` succeeds on a process that has exited and is
    waiting to be reaped, so the kill alone does not prove the attempt
    was still running.
    """
    if attempt.killed and exit_code in (None, -signal.SIGKILL):
        logger.warning(
            f'pull: the attempt for TransitionMessage#{attempt.pk} ran past '
            f'its declared timeout={attempt.timeout_seconds}s and was '
            f'stopped. The error recorded here paces the next claim.'
        )
        _record_attempt_error(
            attempt.pk,
            f'[timeout] the attempt ran past '
            f'timeout={attempt.timeout_seconds}s and was stopped',
        )
        return
    if exit_code in (None, 0):
        return
    logger.warning(
        f'pull: the attempt process for TransitionMessage#{attempt.pk} died '
        f'(exit {exit_code}). Its row lock died with it; the error recorded '
        f'here paces the next claim.'
    )
    _record_attempt_error(
        attempt.pk,
        f'[crashed] the attempt process died (exit {exit_code}) '
        f'before the attempt finished',
    )


def _record_attempt_error(pk: int, message: str) -> None:
    """Record one error on the row — unless the row completed first.

    Another worker on the same queue can claim the row the moment the
    dead attempt's lock is released and finish it before this write. One
    conditional UPDATE keeps the guard and the write in the same
    statement, so a completed row can never take the error.
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
            f'before the error could be recorded; nothing to record.'
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


def run_worker(
    queues: list[str], *, forever: bool = True, concurrency: int = 1,
) -> None:
    """The worker loop: fill the free attempt slots from the claimable
    rows, account for the attempts that end, run the safety nets on
    schedule, wait for a notification, repeat.

    ``concurrency`` is how many attempts this worker runs at a time. One
    worker with several slots shares one memory pool and one set of
    safety nets across them; several one-slot workers each reserve memory
    for their heaviest attempt. Every slot holds its own database
    connection while it runs, so size ``concurrency`` against the
    connection cap (see docs/design/PULL_WORKERS.md).

    ``forever=False`` drains what is claimable now, waits for the
    attempts it started, runs the safety nets, and returns — for tests
    and for a one-off catch-up command.
    """
    logger.info(
        'pull worker starting: queues=%s, attempts at a time=%s',
        ','.join(queues), concurrency,
    )
    attempts: dict[int, _Attempt] = {}
    last_safety_net = 0.0
    while True:
        claimed_any = False
        try:
            while len(attempts) < concurrency:
                pk = claim_next(
                    queues,
                    exclude_pks=[attempt.pk for attempt in attempts.values()],
                )
                if pk is None:
                    break
                _start_attempt(pk, attempts)
                claimed_any = True
        except Exception as exc:
            # A database blip or a failed fork must not end the loop while
            # attempts run: the worker is the only thing that enforces
            # their timeout= and records their crash, so its death would
            # orphan them. The wait below paces the next try.
            logger.error(
                'pull: could not start an attempt (%s: %s). The attempts '
                'already running are still accounted for.',
                type(exc).__name__, exc,
            )
        # A full worker has nothing to do but wait for a slot, so it waits
        # in waitpid. A worker with a free slot must stay reachable: work
        # arrives while a long attempt runs, and the notification wait is
        # what hears it. Blocking here instead would leave those slots idle
        # for the whole life of the longest attempt.
        full = len(attempts) >= concurrency
        _harvest(attempts, block=full)
        if time.monotonic() - last_safety_net >= SAFETY_NET_SECONDS:
            _run_safety_nets()
            last_safety_net = time.monotonic()
        if claimed_any or full:
            continue
        if attempts and not forever:
            # One pass must finish what it started before it returns.
            _harvest(attempts, block=True)
            continue
        if not forever:
            return
        # A shorter wait while attempts run: an attempt that crashes leaves
        # no error on its row, and the row is claimable the moment its lock
        # dies, so the worker should not sit out a full poll interval
        # before recording it.
        _wait_for_work(BUSY_POLL_SECONDS if attempts else POLL_SECONDS)
