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
* :func:`detect_stuck_transitions` — periodic; logs/alerts on messages
  that have hit ``MAX_ERRORS``.

All four are registered under the ``django_logic`` namespace.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from django_logic.background import settings as bg_settings
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from django_logic.logger import logger, transition_logger


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
    cutoff = timezone.now() - timedelta(minutes=bg_settings.retry_minutes())
    max_errors = bg_settings.max_errors()

    queryset = (
        TransitionMessage.objects
        .filter(
            is_completed=False,
            errors_count__lt=max_errors,
            created__lt=cutoff,
        )
        .order_by('created')
    )

    dispatched = 0
    for tm in queryset.iterator():
        try:
            run_background_transition_task.apply_async(
                args=[tm.pk], queue=tm.queue_name
            )
            dispatched += 1
        except Exception as e:
            # A dispatch-layer error (broker down, serialization, etc.)
            # shouldn't stop us from trying the remaining rows.
            logger.error(
                'retry_stale_transitions: failed to dispatch '
                f'TransitionMessage#{tm.pk}: {e}'
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
    """Periodic: log/alert on messages that hit ``MAX_ERRORS`` without completing.

    Returns the number of stuck rows found. Emits one ERROR log line per row.
    """
    max_errors = bg_settings.max_errors()
    stuck = (
        TransitionMessage.objects
        .filter(is_completed=False, errors_count__gte=max_errors)
    )
    count = 0
    for tm in stuck.iterator():
        transition_logger.error(
            f'Stuck transition: TransitionMessage#{tm.pk} '
            f'{tm.app_label}.{tm.model_name}#{tm.instance_id} '
            f'{tm.transition_name} queue={tm.queue_name} '
            f'errors={tm.errors_count} last_error={tm.last_error_message!r}'
        )
        count += 1
    return count
