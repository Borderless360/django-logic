"""Dispatch — where phase 1 hands off to phase 2.

Two modes:

* **Celery mode** (``DJANGO_LOGIC['BACKGROUND_EXECUTION'] = 'celery'``):
  schedule a Celery task on the transition's queue via
  ``transaction.on_commit``. The worker picks it up and runs phase 2.

* **Sync mode** (``'sync'``): run phase 2 inline, immediately after the
  phase-1 atomic block exits. Bypasses ``transaction.on_commit`` so it
  works correctly under Django's ``TestCase`` (which wraps every test
  in a transaction that never commits).

A per-block override is available via :func:`sync_execution`, independent
of the global setting.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from django.db import transaction

from django_logic.background import settings as bg_settings


_force_sync: ContextVar[bool] = ContextVar('_dl_force_sync', default=False)


@contextmanager
def sync_execution():
    """Force Sync mode for the duration of the ``with`` block.

    Useful inside a test / management command when the global setting
    is ``'celery'`` but you want phase 2 to run inline for this block.
    """
    token = _force_sync.set(True)
    try:
        yield
    finally:
        _force_sync.reset(token)


def _current_mode() -> str:
    if _force_sync.get():
        return bg_settings.EXECUTION_SYNC
    return bg_settings.background_execution()


def dispatch_transition(tm) -> None:
    """Hand a fresh TransitionMessage off to phase 2.

    In Celery mode, schedules the Celery task via ``transaction.on_commit``
    so the DB row is visible to the worker.

    In Sync mode, runs phase 2 inline. Exceptions propagate to the caller.
    """
    mode = _current_mode()
    if mode == bg_settings.EXECUTION_SYNC:
        from django_logic.background.runner import run_background_transition
        run_background_transition(tm.pk)
        return

    # Celery mode — deferred import avoids loading the task module (and
    # the app registry work it triggers) on the sync fast path.
    from django_logic.background.observability import task_label
    from django_logic.background.tasks import run_background_transition_task

    _warn_once_about_celery_config(run_background_transition_task)

    # `shadow` gives this dispatch a per-transition name in Celery events /
    # Flower / RabbitMQ management, even though it's the one shared task.
    shadow = task_label(tm)

    def _enqueue():
        run_background_transition_task.apply_async(
            args=[tm.pk], queue=tm.queue_name, shadow=shadow
        )

    transaction.on_commit(_enqueue)


_celery_config_warned = False


def _warn_once_about_celery_config(task) -> None:
    """Warn once, at the first celery-mode dispatch, about Celery config that
    silently breaks the durability contract.

    Checked here rather than at Django app-ready because app-ready runs before
    the project's ``celery.py`` configures the app; by the first dispatch the
    app is configured, making these checks reliable.

    1. **No real broker.** With ``broker_url`` unset Celery falls back to an
       in-memory transport no worker drains: ``apply_async`` succeeds but the
       task never runs, leaving the instance stuck in ``in_progress_state``.
    2. **acks_late without reject_on_worker_lost.** django-logic's task is
       ``acks_late=True`` so a crash re-delivers it — but only if the project
       also sets ``task_reject_on_worker_lost=True``. Without it, a task on a
       worker killed mid-execution (SIGKILL / OOM / deploy) may be
       acked-and-dropped instead of re-delivered; recovery then falls solely
       to the periodic starter (slower). See README → Production deployment.
    """
    global _celery_config_warned
    if _celery_config_warned:
        return
    _celery_config_warned = True
    from django_logic.logger import logger

    try:
        conf = task.app.conf
    except Exception:
        return
    broker = getattr(conf, 'broker_url', None)
    if not broker or str(broker).startswith('memory://'):
        logger.warning(
            "DJANGO_LOGIC['BACKGROUND_EXECUTION']='celery' but the Celery "
            "app has no real broker (broker_url=%r). apply_async publishes "
            "to an in-memory transport no worker consumes, so background "
            "transitions will never run. Configure a durable broker "
            "(Redis/RabbitMQ) or set BACKGROUND_EXECUTION='sync'.",
            broker,
        )
    try:
        acks_late = bool(conf.task_acks_late)
        reject = bool(conf.task_reject_on_worker_lost)
    except Exception:
        return
    if acks_late and not reject:
        logger.warning(
            'django-logic background tasks are acks_late=True, but '
            'CELERY_TASK_REJECT_ON_WORKER_LOST is not set. A worker killed '
            'mid-execution (SIGKILL/OOM/deploy) may then drop the task instead '
            'of re-delivering it; the instance stays in in_progress_state until '
            'the periodic starter recovers it. Set '
            'CELERY_TASK_REJECT_ON_WORKER_LOST=True for prompt crash recovery.'
        )


def retry_pending() -> int:
    """Run one iteration of the periodic starter inline.

    Intended for tests and for management commands that want to simulate
    "time passed, the starter re-dispatched the stale messages".

    Returns the number of messages that were (re-)dispatched.
    """
    from django_logic.background.tasks import _retry_pending_inline
    return _retry_pending_inline()
