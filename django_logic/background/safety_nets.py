"""The safety nets: the stuck finalizer and the cleanup sweep.

Plain functions. The pull worker loop runs them on a fixed cadence
(``pull.run_worker``), so no scheduler has to be configured for them.
Sync-mode tests call them directly, or through ``retry_pending()`` for
the retry pass.

What each one owns:

* :func:`retry_pending` — run every claimable row inline. The visibility
  rule is the same one the pull claim uses: uncompleted, retries left,
  and past the retry wait after the last recorded error. Sync mode's
  "time passed" simulation.
* :func:`detect_stuck_transitions` — finalize rows stuck at
  ``MAX_ERRORS``, and report, once per queue, rows that wait past the
  retry window.
* :func:`cleanup_completed_transitions` — delete old completed rows,
  keeping the newest terminal-failure row per instance and process.

A hanging attempt needs no net here: the worker enforces the declared
``timeout=`` on its attempt process (``pull``), and a dead worker's row
lock dies with its connection.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import DEFAULT_DB_ALIAS, router, transaction
from django.db.models import Count, Min, Q
from django.utils import timezone

from django_logic import conf
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import (
    finalize_stuck_attempt,
    run_background_transition,
)
from django_logic.logger import logger


_CLEANUP_BATCH_SIZE = 1000


def _claimable(queues: list[str] | None = None):
    """Rows a worker may take now. The one place the visibility rule is
    written — the pull claim and the sync retry pass both read it."""
    retry_cutoff = timezone.now() - timedelta(
        minutes=conf.retry_minutes())
    rows = TransitionMessage.objects.filter(
        is_completed=False,
        errors_count__lt=conf.max_errors(),
    ).filter(
        Q(last_error_dt__isnull=True) | Q(last_error_dt__lt=retry_cutoff)
    )
    if queues is not None:
        rows = rows.filter(queue_name__in=queues)
    return rows.order_by('created')


def _queue_is_served(queue_name: str, now, window_minutes: int) -> bool:
    """Whether a worker takes rows from this queue: it started one inside
    the retry window, or it holds one right now.

    A long attempt starts nothing new but keeps its row lock, so the lock
    probe tells a busy worker from a missing one. A dead worker's lock dies
    with its connection.
    """
    recent = now - timedelta(minutes=window_minutes)
    rows = TransitionMessage.objects.filter(queue_name=queue_name)
    # Two ranges of the (is_completed, started_at) index instead of a scan.
    if rows.filter(
        Q(is_completed=True, started_at__gte=recent)
        | Q(is_completed=False, started_at__gte=recent)
    ).exists():
        return True
    started = rows.filter(
        is_completed=False, started_at__isnull=False,
    ).values_list('pk', flat=True)
    return any(TransitionMessage.worker_holds_row(pk) for pk in started)


def retry_pending() -> int:
    """Run every claimable row inline. Returns how many ran cleanly.

    A failing side-effect re-raises out of ``run_background_transition``
    in sync mode; the failure is already recorded on its row, so the scan
    logs it and keeps going. Rows another connection holds are skipped by
    the runner's own row-lock guard.
    """
    ran = 0
    for pk in list(_claimable(None).values_list('pk', flat=True)):
        try:
            run_background_transition(pk)
        except Exception as e:
            logger.warning(f'retry_pending: TransitionMessage#{pk} failed: {e}')
            continue
        ran += 1
    return ran


def detect_stuck_transitions() -> int:
    """Finalize rows stuck at ``MAX_ERRORS``, and report the queues whose
    rows wait past the retry window with no attempt.

    It writes one line per queue, so a long backlog does not write one
    line per row every minute. A queue a worker still takes rows from is
    behind, which is a WARNING. A queue no worker takes rows from is an
    ERROR. Then the worker process is down, its ``--queues`` list misses
    that name, or no deployment started one. Or other work holds all its
    slots.

    Returns the number of rows finalized.
    """
    now = timezone.now()
    report_after = conf.retry_window_minutes()
    never_started = TransitionMessage.objects.filter(
        is_completed=False,
        started_at__isnull=True,
        created__lt=now - timedelta(minutes=report_after),
    )
    waiting = (
        never_started
        .values('queue_name')
        .annotate(rows=Count('pk'), oldest=Min('created'))
        .order_by('queue_name')
    )
    for queue in waiting:
        queue_name, rows = queue['queue_name'], queue['rows']
        oldest_pk = (
            never_started.filter(queue_name=queue_name)
            .order_by('created', 'pk')
            .values_list('pk', flat=True)
            .first()
        )
        age_minutes = int((now - queue['oldest']).total_seconds() // 60)
        if _queue_is_served(queue_name, now, report_after):
            waited = f'{rows} rows have' if rows > 1 else '1 row has'
            logger.warning(
                f'detect_stuck_transitions: queue {queue_name!r} is behind. '
                f'{waited} waited longer than {report_after} minutes; '
                f'the oldest, TransitionMessage#{oldest_pk}, has waited '
                f'{age_minutes} minutes. A worker takes rows from this queue, '
                f'so they run in turn.'
            )
            continue
        behind = f' {rows - 1} more rows wait behind it.' if rows > 1 else ''
        logger.error(
            f'detect_stuck_transitions: TransitionMessage#{oldest_pk} has '
            f'waited {age_minutes} minutes on queue {queue_name!r}, and no '
            f'worker has picked up a row from that queue for {report_after} '
            f'minutes.{behind} Does a worker serve that queue? Start one '
            f'(dl_worker --queues {queue_name}); it takes the rows at once. '
            f'If one runs, other work holds all its slots.'
        )

    max_errors = conf.max_errors()
    stuck_ids = list(
        TransitionMessage.objects
        .filter(is_completed=False, errors_count__gte=max_errors)
        .values_list('pk', flat=True)
    )
    finalized = 0
    for pk in stuck_ids:
        try:
            if finalize_stuck_attempt(pk):
                finalized += 1
        except Exception as e:
            # One bad row must not stop the scan.
            logger.error(
                f'detect_stuck_transitions: failed to finalize '
                f'TransitionMessage#{pk}: {e}'
            )
    return finalized


def cleanup_completed_transitions() -> int:
    """Delete completed rows older than ``CLEANUP_DAYS``.

    A row that ended in terminal failure is the only explanation for an
    instance parked in its ``failed_state``, so the sweep keeps the newest
    such row per instance and process and deletes the rest.

    Each batch commits separately, so a stopped maintenance process keeps
    earlier progress. A caller's outer transaction still controls its commit.
    """
    from django.db.models import OuterRef, Subquery

    cutoff = timezone.now() - timedelta(days=conf.cleanup_days())
    # ended_in_failure, not an errors_count comparison: a permanent failure
    # completes at one error, and a retried success can carry several, so
    # the count cannot tell them apart. Every terminal-failure path sets
    # the flag; a superseded row does not — the external state change won
    # and the instance is not parked.
    failed = Q(ended_in_failure=True)
    alias = router.db_for_write(TransitionMessage) or DEFAULT_DB_ALIAS
    rows = TransitionMessage.objects.using(alias)
    newest_failed = (
        rows
        .filter(
            failed,
            is_completed=True,
            app_label=OuterRef('app_label'),
            model_name=OuterRef('model_name'),
            instance_id=OuterRef('instance_id'),
            process_name=OuterRef('process_name'),
        )
        .order_by('-completed_at', '-pk')
        .values('pk')[:1]
    )
    eligible = (
        rows.filter(is_completed=True, modified__lt=cutoff)
        .exclude(failed & Q(pk=Subquery(newest_failed)))
    )
    deleted = 0
    while True:
        with transaction.atomic(using=alias):
            message_ids = list(
                eligible.order_by('pk').values_list('pk', flat=True)[:_CLEANUP_BATCH_SIZE]
            )
            if not message_ids:
                break
            # Recheck eligibility because a row can change after selection.
            count, _ = eligible.filter(pk__in=message_ids).delete()
        deleted += count
    if deleted:
        logger.info(f'cleanup_completed_transitions: deleted {deleted} rows')
    return deleted
