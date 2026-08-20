"""Dispatch — where enqueue hands off to the worker.

Two modes:

* **Celery mode** (``DJANGO_LOGIC['BACKGROUND_EXECUTION'] = 'celery'``):
  schedule a Celery task on the transition's queue via
  ``transaction.on_commit``. The worker picks it up and executes.

* **Sync mode** (``'sync'``): execute inline, immediately after the
  enqueue atomic block exits. Bypasses ``transaction.on_commit`` so it
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
    is ``'celery'`` but you want the worker path to run inline for this
    block.
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


def dispatch_transition(transition_message) -> None:
    """Hand a fresh TransitionMessage off to the worker.

    In Celery mode, schedules the Celery task via ``transaction.on_commit``
    so the DB row is visible to the worker.

    In Sync mode, executes inline. Exceptions propagate to the caller.
    """
    mode = _current_mode()
    if mode == bg_settings.EXECUTION_SYNC:
        from django_logic.background.runner import run_background_transition
        run_background_transition(transition_message.pk)
        return

    if mode == bg_settings.EXECUTION_PULL:
        # The committed row is the signal. The notification only wakes the
        # workers early; losing it costs one poll interval.
        from django_logic.background.pull import notify_workers
        transaction.on_commit(notify_workers)
        return

    # Celery mode — deferred import avoids loading the task module (and
    # the app registry work it triggers) on the sync fast path.
    from django_logic.background.observability import task_label
    from django_logic.background.tasks import run_background_transition_task

    _warn_once_about_celery_config(run_background_transition_task)

    # `shadow` gives this dispatch a per-transition name in Celery events /
    # Flower / RabbitMQ management, even though it's the one shared task.
    shadow = task_label(transition_message)

    def _enqueue():
        # The primary publish counts as the first dispatch, so the
        # starter's claim window starts from here, not from the first tick.
        # The count goes back if the publish raises — the ceiling counts
        # only messages the broker really took.
        from django_logic.background.models import TransitionMessage
        TransitionMessage.mark_dispatched(transition_message.pk)
        try:
            run_background_transition_task.apply_async(
                args=[transition_message.pk],
                queue=transition_message.queue_name,
                shadow=shadow,
            )
        except Exception:
            TransitionMessage.publish_failed(transition_message.pk)
            raise

    transaction.on_commit(_enqueue)


_celery_config_warned = False


def _warn_once_about_celery_config(task) -> None:
    """Warn once, at the first celery-mode dispatch, about Celery config that
    silently breaks the durability contract.

    Checked here rather than at Django app-ready because app-ready runs before
    the project's ``celery.py`` configures the app; by the first dispatch the
    app is configured, making the check reliable.

    **No real broker.** With ``broker_url`` unset Celery falls back to an
    in-memory transport no worker drains: ``apply_async`` succeeds but the
    task never runs, leaving the instance stuck in ``in_progress_state``.

    Every django-logic task sets ``reject_on_worker_lost=True`` alongside
    ``acks_late=True``, so a worker crash redelivers the message.
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


def retry_pending() -> int:
    """Run one iteration of the periodic starter inline.

    Intended for tests and for management commands that want to simulate
    "time passed, the starter re-dispatched the stale messages".

    Returns the number of messages that were (re-)dispatched.
    """
    from django_logic.background.tasks import _retry_pending_inline
    return _retry_pending_inline()


def in_flight(instance, process_name: str = 'process') -> bool:
    """Whether a background transition is still being retried for
    ``instance`` + ``process_name`` — an uncompleted ``TransitionMessage``
    exists and is inside its retry window.

    For shaping answers at API seams ("busy, try again shortly"), NOT as a
    pre-flight gate: the read is racy — a transition can start or complete
    between this call and whatever the caller does next. The engine's own
    guards (enqueue's unique constraint, the sync gate's under-lock check)
    stay authoritative.

    A stranded row (nothing is retrying it) answers ``False``: it is not
    "busy, retry shortly", and the engine's gates raise the plain
    ``TransitionNotAllowed`` for it — so a consumer answering 409 on this
    probe and 400 on the plain base stays consistent. The engine's own
    failure-path write-skip deliberately uses bare existence instead —
    it must never clobber an uncompleted row's instance, stranded or not.

    Returns ``False`` when ``django_logic.background`` is not installed.
    """
    from django.apps import apps

    if not apps.is_installed('django_logic.background'):
        return False
    from django_logic.background.models import TransitionMessage

    return (
        TransitionMessage.retry_status(instance, process_name)
        == TransitionMessage.RETRYING
    )
