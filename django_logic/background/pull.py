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

The worker runs the safety nets in one separate process with a time limit.
This keeps consumer callbacks out of the supervisor. Nothing needs an
external schedule.
"""
from __future__ import annotations

import os
import select
import signal
import time
from collections import deque
from dataclasses import dataclass

from django.db import (DEFAULT_DB_ALIAS, Error, connections, router,
                       transaction)

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

#: Check child exits promptly while a full worker waits for a free slot.
CHILD_POLL_SECONDS = 0.01

# Stop claiming when too many error writes are waiting. Existing children
# can still finish, so the maximum also includes the worker's concurrency.
MAX_PENDING_ACCOUNTING = 1000
ACCOUNTING_RETRIES_PER_PASS = 16
ACCOUNTING_WARNING_SECONDS = 5.0
ACCOUNTING_WARNING_REPEAT_SECONDS = 60.0

#: How often the loop runs the safety nets (stuck report, cleanup).
SAFETY_NET_SECONDS = 60.0
SAFETY_NET_TIMEOUT_SECONDS = 60.0


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
    failing one. The call returns only after the failure is recorded,
    so a caller never loses that record.
    """
    from django_logic.background.runner import run_background_transition

    pk = claim_next(queues)
    if pk is None:
        return False
    if isolate and hasattr(os, 'fork'):
        attempts: dict[int, _Attempt] = {}
        pending: deque[_Attempt] = deque()
        _start_attempt(pk, attempts)
        while attempts or pending:
            _harvest(attempts, pending, block=True)
    else:
        run_background_transition(pk)
    return True


@dataclass
class _Attempt:
    """One attempt, kept until its child exits and its error is recorded."""

    # A safety-net pass has no message row.
    pk: int | None
    timeout_seconds: float | None
    deadline: float | None
    killed: bool = False
    reaped: bool = False
    exit_code: int | None = None
    next_retry_at: float = 0.0
    deferred_since: float | None = None
    last_deferred_warning: float | None = None


def _start_attempt(pk: int | None, attempts: dict[int, _Attempt]) -> None:
    """Fork one attempt or safety-net pass and track its deadline.

    The worker and the attempt process must not share a database
    connection — a connection closed (or crashed) on one side poisons
    the other's session. The worker closes every connection before the
    fork; each side then opens its own lazily.
    """
    from django_logic.background.models import TransitionMessage
    from django_logic.background.runner import run_background_transition

    timeout_seconds = SAFETY_NET_TIMEOUT_SECONDS if pk is None else (
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
            if pk is None:
                _run_safety_nets()
            else:
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


def _poll_delay(attempts: dict[int, _Attempt], maximum: float) -> float:
    now = time.monotonic()
    for attempt in attempts.values():
        if attempt.reaped:
            continue
        if attempt.killed:
            # Collect a stopped process's exit status before another long wait.
            maximum = min(maximum, 0.01)
        elif attempt.deadline is not None:
            maximum = min(maximum, max(0.0, attempt.deadline - now))
    return maximum


def _stop_overdue(attempts: dict[int, _Attempt]) -> None:
    now = time.monotonic()
    for pid, attempt in attempts.items():
        if attempt.killed or attempt.deadline is None or now < attempt.deadline:
            continue
        attempt.killed = True
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            # The child ended between the deadline check and the signal.
            pass


def _finish_attempt(attempt: _Attempt, pending: deque[_Attempt]) -> None:
    attempt.reaped = True
    attempt.deadline = None
    if not _try_account(attempt):
        attempt.next_retry_at = time.monotonic() + BUSY_POLL_SECONDS
        pending.append(attempt)


def _retry_accounting(attempts, pending, budget, wait_until):
    # Retry times increase as entries join the queue. Rotating failed writes
    # gives each retained attempt a turn without keeping a recyclable PID.
    while pending and budget:
        now = time.monotonic()
        if pending[0].next_retry_at > now or (wait_until is not None and now >= wait_until):
            break
        _stop_overdue(attempts)
        attempt = pending.popleft()
        if not _try_account(attempt):
            attempt.next_retry_at = time.monotonic() + BUSY_POLL_SECONDS
            pending.append(attempt)
        budget -= 1
    return budget


def _harvest(
    attempts: dict[int, _Attempt], pending: deque[_Attempt], *,
    block: bool, max_wait: float | None = None,
) -> None:
    """Collect process exit statuses, enforce deadlines, and record failures.

    Running job processes are tracked by process ID. Finished processes leave
    that dictionary immediately, even when a database lock delays the record.
    Unsaved failure records keep the message ID and do not count as running jobs.

    A blocking call returns after a process exits or its wait limit passes.
    Each call limits retry work so other jobs can continue.
    """
    wait_until = None if max_wait is None else time.monotonic() + max_wait
    budget = ACCOUNTING_RETRIES_PER_PASS
    while attempts or pending:
        _stop_overdue(attempts)
        budget = _retry_accounting(attempts, pending, budget, wait_until)
        if not attempts and not pending:
            return
        now = time.monotonic()
        remaining = None if wait_until is None else max(0.0, wait_until - now)
        if not attempts:
            if not block or remaining == 0 or budget == 0:
                return
            delay = max(0.0, pending[0].next_retry_at - now)
            time.sleep(delay if remaining is None else min(delay, remaining))
            continue
        next_deadline = min(
            (attempt.deadline for attempt in attempts.values()
             if attempt.deadline is not None and not attempt.killed),
            default=None,
        )
        try:
            pid, raw_status = os.waitpid(
                -1, 0 if block and next_deadline is None
                and not pending and wait_until is None else os.WNOHANG,
            )
        except ChildProcessError:
            # Another handler collected these exit statuses; none will arrive here.
            ended = list(attempts.values())
            attempts.clear()
            for attempt in ended:
                attempt.exit_code = None
                _finish_attempt(attempt, pending)
            continue
        if pid == 0:
            if not block or remaining == 0 or budget == 0:
                return
            delay = CHILD_POLL_SECONDS if remaining is None else min(CHILD_POLL_SECONDS, remaining)
            time.sleep(_poll_delay(attempts, delay))
            continue
        attempt = attempts.pop(pid, None)
        if attempt is None:
            # Consumer code can leave a child which this worker did not start.
            continue
        attempt.exit_code = os.waitstatus_to_exitcode(raw_status)
        _finish_attempt(attempt, pending)
        if block:
            return


def _try_account(attempt: _Attempt) -> bool:
    """Record how a finished job process ended. Return whether the write succeeded.

    The write must not raise out of the worker loop: the database that
    refuses it is often the same one whose outage crashed the attempt,
    and a worker that dies here leaves its other attempts running with
    nothing to enforce their ``timeout=``.
    """
    try:
        _account(attempt, attempt.exit_code)
    except Exception as exc:
        cause = exc.__cause__
        if (getattr(cause, 'sqlstate', None)
                or getattr(cause, 'pgcode', None)) == '55P03':
            now = time.monotonic()
            if attempt.deferred_since is None:
                attempt.deferred_since = now
            waited = now - attempt.deferred_since
            if (waited >= ACCOUNTING_WARNING_SECONDS
                    and (attempt.last_deferred_warning is None
                         or now - attempt.last_deferred_warning >= ACCOUNTING_WARNING_REPEAT_SECONDS)):
                logger.warning(
                    'pull: recording the failure for TransitionMessage#%s has waited %.1fs '
                    'for a row lock. The worker retains the error and retries; '
                    'check the PostgreSQL row holder if the wait continues.',
                    attempt.pk, waited,
                    extra={'transition_message_pk': attempt.pk, 'wait_seconds': waited},
                )
                attempt.last_deferred_warning = now
            return False
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
    ``exit_code`` is ``None`` when another handler collected the exit status,
    so no status reached this worker.

    An attempt process that dies without completing the row left no
    error on it, so the worker records one: the claim's retry wait then
    paces the next attempt, and ``MAX_ERRORS`` bounds a crash loop — a
    side-effect that crashes (or hangs) every time ends in
    ``failed_state`` like one that fails every time, instead of looping
    forever.

    A process stopped after its deadline counts as a timeout even when its
    exit status is unavailable. If the status shows it finished on its own,
    do not record a timeout. ``os.kill`` can succeed after a process exits,
    so its result alone does not prove that the process was still running.
    """
    if attempt.pk is None:
        _report_safety_net_result(attempt, exit_code)
        return
    if attempt.killed and exit_code in (None, -signal.SIGKILL):
        _record_attempt_error(
            attempt.pk,
            f'[timeout] the attempt ran past '
            f'timeout={attempt.timeout_seconds}s and was stopped',
        )
        logger.warning(
            f'pull: the attempt for TransitionMessage#{attempt.pk} ran past '
            f'its declared timeout={attempt.timeout_seconds}s and was '
            f'stopped. The error recorded here paces the next claim.'
        )
        return
    if exit_code in (None, 0):
        return
    _record_attempt_error(
        attempt.pk,
        f'[crashed] the attempt process died (exit {exit_code}) '
        f'before the attempt finished',
    )
    logger.warning(
        f'pull: the attempt process for TransitionMessage#{attempt.pk} died '
        f'(exit {exit_code}). Its row lock died with it; the error recorded '
        f'here paces the next claim.'
    )


def _record_attempt_error(pk: int, message: str) -> None:
    """Record one error on the row — unless the row completed first.

    Another worker on the same queue can claim the row the moment the
    dead attempt's lock is released. Do not wait for that worker: this
    process must still enforce its other attempts' deadlines.
    The conditional update protects a row that already completed.
    """
    from django.db.models import F
    from django.utils import timezone

    from django_logic.background.models import TransitionMessage, db_safe_text

    alias = router.db_for_write(TransitionMessage) or DEFAULT_DB_ALIAS
    rows = TransitionMessage.objects.using(alias).filter(pk=pk)
    with transaction.atomic(using=alias):
        rows.select_for_update(nowait=True).values_list('pk', flat=True).first()
        now = timezone.now()
        updated = rows.filter(is_completed=False).update(
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


def _report_safety_net_result(attempt: _Attempt, exit_code: int | None) -> None:
    if attempt.killed and exit_code in (None, -signal.SIGKILL):
        logger.warning(
            'pull: safety nets exceeded their %ss limit and were stopped. '
            'Uncompleted rows will be checked on the next pass.',
            attempt.timeout_seconds,
        )
    elif exit_code not in (None, 0):
        logger.warning(
            'pull: safety nets stopped with exit code %s. '
            'Uncompleted rows will be checked on the next pass.',
            exit_code,
        )


def _wait_for_work(timeout: float) -> None:
    """Sleep until a notification arrives or ``timeout`` passes.

    Holds one LISTEN connection per worker process. ``LISTEN`` lasts for
    the session, so it is issued once per connection, not per wait.
    psycopg 2 and 3 expose notifications differently, so the wait
    branches on the driver: psycopg 2 keeps a ``notifies`` list the
    caller drains after ``select``; psycopg 3 waits inside the
    ``notifies()`` generator. When the connection cannot listen (a
    pooler that rejects LISTEN, a broken socket), the wait degrades to
    a plain sleep and the poll floor carries the loop. Only a database,
    driver or socket error degrades. Anything else is raised, so a
    defect in this function cannot run unseen.
    """
    from django_logic.background.models import TransitionMessage

    alias = router.db_for_write(TransitionMessage) or DEFAULT_DB_ALIAS
    connection = connections[alias]
    # The LISTEN and the wait run on the raw connection, so they raise
    # the driver's own errors, not the ones Django wraps. The degraded
    # path names both, and OSError for a socket select cannot wait on.
    cannot_listen = (Error, connection.Database.Error, OSError)
    try:
        connection.ensure_connection()
        raw = connection.connection
        # The flag lives on the Django wrapper. psycopg2's connection is
        # a C type that takes no new attribute, so a flag kept there is
        # lost and the worker listens again on every wait. The flag holds
        # the connection it listened on: a reconnect gives a new object,
        # which listens again on its own session.
        if getattr(connection, '_django_logic_listening', None) is not raw:
            with raw.cursor() as cursor:
                cursor.execute(f'LISTEN {NOTIFY_CHANNEL}')
            connection._django_logic_listening = raw
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
    except cannot_listen as exc:
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

    One additional process runs safety nets with a 60-second limit.
    It does not use an attempt slot. Reserve a connection for it too.

    ``forever=False`` drains what is claimable now, waits for the
    attempts it started, runs the safety nets, and returns — for tests
    and for a one-off catch-up command.
    """
    logger.info(
        'pull worker starting: queues=%s, attempts at a time=%s',
        ','.join(queues), concurrency,
    )
    attempts: dict[int, _Attempt] = {}
    pending: deque[_Attempt] = deque()
    last_safety_net = None
    while True:
        claimed_any = False
        try:
            while (sum(attempt.pk is not None for attempt in attempts.values()) < concurrency
                   and len(pending) < MAX_PENDING_ACCOUNTING):
                pk = claim_next(
                    queues,
                    exclude_pks=[attempt.pk for attempt in attempts.values()
                                 if attempt.pk is not None] + [attempt.pk for attempt in pending],
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
        # A full worker still checks deadlines and schedules safety nets.
        before_harvest = len(attempts) + len(pending)
        before_children = len(attempts)
        _harvest(attempts, pending, block=False)
        harvested = (len(attempts) < before_children
                     or len(attempts) + len(pending) < before_harvest)
        now = time.monotonic()
        safety_net_running = any(attempt.pk is None for attempt in attempts.values())
        if (not safety_net_running
                and (last_safety_net is None or forever or claimed_any)
                and (last_safety_net is None or now - last_safety_net >= SAFETY_NET_SECONDS)):
            try:
                _start_attempt(None, attempts)
            except Exception as exc:
                logger.error('pull: could not start safety nets: %s', exc)
            last_safety_net = now
        if claimed_any or harvested:
            continue
        full = sum(attempt.pk is not None for attempt in attempts.values()) >= concurrency
        if (attempts or pending) and (full or not forever or len(pending) >= MAX_PENDING_ACCOUNTING):
            # Child exits do not notify PostgreSQL. A full worker waits for
            # them directly, but returns in time to schedule maintenance.
            max_wait = None
            if forever:
                max_wait = BUSY_POLL_SECONDS
                if not safety_net_running:
                    max_wait = min(max_wait, max(
                        0.0, last_safety_net + SAFETY_NET_SECONDS - time.monotonic(),
                    ))
            _harvest(attempts, pending, block=True, max_wait=max_wait)
            continue
        if not forever:
            return
        # A shorter wait while attempts run: an attempt that crashes leaves
        # no error on its row, and the row is claimable the moment its lock
        # dies, so the worker should not sit out a full poll interval
        # before recording it.
        delay = BUSY_POLL_SECONDS if attempts or pending else POLL_SECONDS
        if pending:
            delay = min(delay, max(0.0, pending[0].next_retry_at - time.monotonic()))
        _wait_for_work(_poll_delay(attempts, delay))
