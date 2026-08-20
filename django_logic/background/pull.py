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

Spike status: additive and off by default. The watchdog and the cleanup
still run, called from inside the worker loop, so pull mode needs no
beat schedule at all.
"""
from __future__ import annotations

import select
import time

from django.db import DEFAULT_DB_ALIAS, connections, router, transaction

from django_logic.background import settings as bg_settings
from django_logic.logger import logger

#: One channel for every queue. The notification carries no payload and
#: means only "ask the database now"; the claim's queue filter does the
#: routing, so per-queue channels would buy nothing.
NOTIFY_CHANNEL = 'django_logic_work'

#: The floor under LISTEN/NOTIFY: a worker asks the database at least
#: this often even when no notification arrives.
POLL_SECONDS = 5.0

#: How often the loop runs the safety nets (watchdog, stuck report,
#: cleanup) that beat used to schedule.
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
    from django_logic.background.models import TransitionMessage
    from django.db.models import Q
    from django.utils import timezone
    from datetime import timedelta

    retry_cutoff = timezone.now() - timedelta(
        minutes=bg_settings.retry_minutes())
    with transaction.atomic():
        return (
            TransitionMessage.objects
            .select_for_update(skip_locked=True)
            .filter(
                is_completed=False,
                queue_name__in=queues,
                errors_count__lt=bg_settings.max_errors(),
            )
            .filter(
                Q(last_error_dt__isnull=True)
                | Q(last_error_dt__lt=retry_cutoff)
            )
            .order_by('created')
            .values_list('pk', flat=True)
            .first()
        )


def run_once(queues: list[str]) -> bool:
    """Claim and execute at most one row. Returns whether one ran."""
    from django_logic.background.runner import run_background_transition

    pk = claim_next(queues)
    if pk is None:
        return False
    run_background_transition(pk)
    return True


def _run_safety_nets() -> None:
    """The periodic work beat used to own: abandoned-attempt watchdog,
    the stuck finalizer and its never-started report, and the cleanup
    sweep. Called from the loop, so pull mode needs no beat process."""
    from django_logic.background.tasks import (
        _watchdog_stale_attempts_inline,
        cleanup_completed_transitions,
        detect_stuck_transitions,
    )

    for step in (
        _watchdog_stale_attempts_inline,
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

    Holds one LISTEN connection per worker process. When the connection
    cannot listen (a pooler that rejects LISTEN, a broken socket), the
    wait degrades to a plain sleep and the poll floor carries the loop.
    """
    from django_logic.background.models import TransitionMessage

    alias = router.db_for_write(TransitionMessage) or DEFAULT_DB_ALIAS
    try:
        connection = connections[alias]
        connection.ensure_connection()
        raw = connection.connection
        with raw.cursor() as cursor:
            cursor.execute(f'LISTEN {NOTIFY_CHANNEL}')
        select.select([raw], [], [], timeout)
        raw.poll()
        raw.notifies.clear()
    except Exception:
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
        while run_once(queues):
            ran_any = True
        now = time.monotonic()
        if now - last_safety_net >= SAFETY_NET_SECONDS:
            _run_safety_nets()
            last_safety_net = now
        if not forever:
            return
        if not ran_any:
            _wait_for_work(POLL_SECONDS)
