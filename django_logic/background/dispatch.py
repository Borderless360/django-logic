"""Dispatch — where enqueue hands off to the worker.

Two modes:

* **Pull mode** (``DJANGO_LOGIC['BACKGROUND_EXECUTION'] = 'pull'``, the
  default): the committed row is the signal. Enqueue fires one
  payload-free notification after commit so a waiting worker asks the
  database at once; a lost notification costs one poll interval. The
  worker loop lives in :mod:`django_logic.background.pull`.

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
    is ``'pull'`` but you want the worker path to run inline for this
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

    In Pull mode, notify the workers after commit — the committed row is
    what they run, so there is nothing to lose or duplicate.

    In Sync mode, execute inline. Exceptions propagate to the caller.
    """
    if _current_mode() == bg_settings.EXECUTION_SYNC:
        from django_logic.background.runner import run_background_transition
        run_background_transition(transition_message.pk)
        return

    from django_logic.background.pull import notify_workers
    transaction.on_commit(notify_workers)


def retry_pending() -> int:
    """Run every claimable row inline, once.

    For tests and management commands that want to simulate "time
    passed, the retry wait is over". Returns the number of rows that ran
    cleanly.
    """
    from django_logic.background.safety_nets import run_pending
    return run_pending()


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
