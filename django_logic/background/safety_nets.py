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
  ``MAX_ERRORS``, and report rows that no worker has ever picked up.
* :func:`cleanup_completed_transitions` — delete old completed rows,
  keeping the newest terminal-failure row per instance and process.

A hanging attempt needs no net here: the worker enforces the declared
``timeout=`` on its attempt process (``pull``), and a dead worker's row
lock dies with its connection.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from django_logic import conf
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import (
    finalize_stuck_attempt,
    run_background_transition,
)
from django_logic.logger import logger


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
            logger.error(f'retry_pending: TransitionMessage#{pk} failed: {e}')
            continue
        ran += 1
    return ran


def detect_stuck_transitions() -> int:
    """Finalize rows stuck at ``MAX_ERRORS``, and name the rows no worker
    has ever picked up.

    A row that sits unstarted past the retry window means no worker
    serves its queue — the worker process is down, its ``--queues`` list
    is missing that name, or the deployment never started one. The
    library cannot fix that, but it can say it, which is the one thing
    that was missing when five silent rows sat on a staging database.

    Returns the number of rows finalized.
    """
    now = timezone.now()
    report_after = max(
        conf.retry_minutes() * (conf.max_errors() + 1), 15,
    )
    never_started = (
        TransitionMessage.objects
        .filter(
            is_completed=False,
            started_at__isnull=True,
            created__lt=now - timedelta(minutes=report_after),
        )
        .values_list('pk', 'queue_name', 'created')
    )
    for pk, queue_name, created in never_started:
        age_minutes = int((now - created).total_seconds() // 60)
        logger.error(
            f'detect_stuck_transitions: TransitionMessage#{pk} has waited '
            f'{age_minutes} minutes on queue {queue_name!r} and no worker '
            f'has picked it up — does a worker serve that queue? Start one '
            f'(dl_worker --queues {queue_name}); it takes the row at once.'
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
    such row per instance and process and deletes the rest. One row per
    parked instance stays, however late the investigation comes.
    """
    from django.db.models import OuterRef, Subquery

    cutoff = timezone.now() - timedelta(days=conf.cleanup_days())
    # ended_in_failure, not an errors_count comparison: a permanent failure
    # completes at one error, and a retried success can carry several, so
    # the count cannot tell them apart. Every terminal-failure path sets
    # the flag; a superseded row does not — the external state change won
    # and the instance is not parked.
    failed = Q(ended_in_failure=True)
    newest_failed = (
        TransitionMessage.objects
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
    with transaction.atomic():
        deleted, _ = (
            TransitionMessage.objects
            .filter(is_completed=True, modified__lt=cutoff)
            .exclude(failed & Q(pk=Subquery(newest_failed)))
            .delete()
        )
    if deleted:
        logger.info(f'cleanup_completed_transitions: deleted {deleted} rows')
    return deleted
