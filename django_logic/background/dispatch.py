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
    app is configured, making the check reliable.

    **No real broker.** With ``broker_url`` unset Celery falls back to an
    in-memory transport no worker drains: ``apply_async`` succeeds but the
    task never runs, leaving the instance stuck in ``in_progress_state``.

    (The old acks_late/reject_on_worker_lost warning is gone: it read the
    *global* ``conf.task_acks_late`` and so never fired for the per-task
    ``acks_late=True`` that actually creates the hazard — issue #91. The
    hazard itself is now eliminated at the source: every django-logic task
    sets ``reject_on_worker_lost=True`` alongside ``acks_late=True``.)
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


STRANDED_MARKER = '[stranded]'


def recover_stranded_states() -> int:
    """Drive provably-stranded instances out of their ``in_progress_state``
    (#136).

    A hard-killed **synchronous** transition (worker OOM / SIGKILL / dyno
    eviction mid side-effect) leaves its instance parked in the
    transition's ``in_progress_state``. The state lock self-expires after
    ``LOCK_TIMEOUT`` and the implicit-source rule keeps the transition
    re-drivable — but nothing *acts*: no failure hooks run, no counter
    increments, no alert fires, and the instance sits until a human
    notices. Background transitions never need this sweep: their
    ``TransitionMessage`` row is the durable record the retry starter /
    watchdog / stuck finalizer already act on.

    Stranded means **all** of:

    * the instance sits in a transition's declared ``in_progress_state``;
    * the state lock is **not** held — a live sync execution holds the
      lock for its whole run, so an expired/absent lock means the holder
      died (or the lock outlived ``LOCK_TIMEOUT``);
    * the instance has **no uncompleted** ``TransitionMessage`` — an
      in-flight background transition is the starter's job, not ours
      (checked per instance, not per process: conservative when several
      processes share a model).

    Each stranded instance is driven through the owning transition's
    normal failure path — ``failed_state`` write, failure side-effects,
    failure callbacks — with a synthetic ``[stranded]`` error, so the
    standard alerting/retry paths apply. A stranded instance whose
    transition declares no ``failed_state`` is logged loudly and left
    untouched (it stays re-drivable via the implicit source).

    Returns the number of instances recovered.
    """
    from django_logic.coverage import iter_bound_transitions
    from django_logic.background.models import TransitionMessage
    from django_logic.logger import logger
    from django_logic.state import State

    recovered = 0
    seen = set()
    for binding, process_cls, transition in iter_bound_transitions():
        in_progress = getattr(transition, 'in_progress_state', None)
        if not in_progress:
            continue
        key = (binding.model._meta.label, binding.state_field,
               in_progress, transition.action_name)
        if key in seen:
            continue
        seen.add(key)

        candidates = binding.model.objects.filter(
            **{binding.state_field: in_progress}
        )
        for instance in candidates.iterator():
            state = State(instance, binding.state_field)
            if state.is_locked():
                continue  # a live execution still holds it
            in_flight = TransitionMessage.objects.filter(
                app_label=instance._meta.app_label,
                model_name=instance._meta.model_name,
                instance_id=str(instance.pk),
                is_completed=False,
            ).exists()
            if in_flight:
                continue  # background machinery owns this one

            label = (f'{binding.model._meta.label}#{instance.pk} '
                     f'{binding.state_field}={in_progress!r} '
                     f'({transition.action_name})')
            if not transition.failed_state:
                logger.warning(
                    f'recover_stranded_states: {label} is stranded but the '
                    f'transition declares no failed_state — left as-is '
                    f'(re-drivable via the implicit in-progress source).'
                )
                continue
            try:
                transition.fail_transition(
                    state,
                    RuntimeError(
                        f'{STRANDED_MARKER} recovered by '
                        f'recover_stranded_states: the process died '
                        f'mid-transition and left this instance in '
                        f'{in_progress!r}.'
                    ),
                    tr_id='stranded-recovery',
                )
                logger.error(
                    f'recover_stranded_states: recovered {label} -> '
                    f'{transition.failed_state!r}'
                )
                recovered += 1
            except Exception as e:
                # One bad instance must not stop the sweep.
                logger.error(
                    f'recover_stranded_states: failed to recover {label}: {e}'
                )
    return recovered
