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

    # Celery mode — lazy import keeps Celery strictly optional.
    from django_logic.background.tasks import run_background_transition_task

    def _enqueue():
        run_background_transition_task.apply_async(
            args=[tm.pk], queue=tm.queue_name
        )

    transaction.on_commit(_enqueue)


def retry_pending() -> int:
    """Run one iteration of the periodic starter inline.

    Intended for tests and for management commands that want to simulate
    "time passed, the starter re-dispatched the stale messages".

    Returns the number of messages that were (re-)dispatched.
    """
    from django_logic.background.tasks import _retry_pending_inline
    return _retry_pending_inline()
