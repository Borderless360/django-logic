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

    A candidate is an instance sitting in a transition's declared
    ``in_progress_state`` with no uncompleted ``TransitionMessage``
    **for that process** (same scope as
    ``_ensure_no_background_in_flight`` and the partial unique
    constraint — an in-flight background job on a *different* bound
    process must not delay recovery). ``Action``\\ s are never
    candidates: an Action accepts ``in_progress_state`` but only as an
    implicit source — it never *writes* it, so it cannot have stranded
    an instance there. (Its ``fail_transition`` also holds no lock: it
    neither unlocks nor writes ``failed_state`` while the state is
    locked, so the ownership-transfer contract below would not hold for
    it.) Recovery itself runs **under the state lock** with the same
    contract as the phase-2 state guard (a manual fix or a re-drive that
    wins the race always wins — see
    :func:`_recover_stranded_instance`), so the sweep never clobbers
    live work or steals another caller's lock — as long as live runs
    finish within their lock TTL. A synchronous run that *outlives* its
    lock is indistinguishable from a stranded one (this TTL-expiry
    hazard predates the sweep; the sweep makes acting on it prompt).
    Size the global ``LOCK_TIMEOUT`` above your longest synchronous
    side-effect, or give legitimately long transitions (report
    generation, large exports) their own
    ``Transition(..., lock_timeout=...)``.

    Each recovered instance goes through the owning transition's normal
    failure path — ``failed_state`` write, failure side-effects, failure
    callbacks — with a synthetic ``[stranded]`` error, so the standard
    alerting/retry paths apply. A stranded instance whose transition
    declares no ``failed_state`` is logged loudly and left untouched (it
    stays re-drivable via the implicit source).

    Returns the number of instances recovered.
    """
    from django_logic.coverage import iter_bound_transitions
    from django_logic.logger import logger
    from django_logic.process import collect_ambiguous_in_progress_states
    from django_logic.transition import Action

    recovered = 0
    seen = set()
    # In-progress states claimed by more than one bound machine have no
    # provenance for a record-less stranding — recovering would guess an
    # owner and could run the wrong failed_state/hooks. Skip them loudly;
    # the django_logic.E001 system check flags the topology itself (#143).
    ambiguous = collect_ambiguous_in_progress_states()
    for key in sorted(ambiguous):
        model_label, state_field, in_progress = key
        logger.error(
            f'recover_stranded_states: in_progress_state {in_progress!r} '
            f'on {model_label}.{state_field} is claimed by multiple bound '
            f'machines (django_logic.E001); skipping recovery for it — '
            f'stranded instances stay parked until the binding topology '
            f'is fixed.'
        )
    for binding, process_cls, transition in iter_bound_transitions():
        in_progress = getattr(transition, 'in_progress_state', None)
        if not in_progress:
            continue
        if (binding.model._meta.label, binding.state_field, in_progress) in ambiguous:
            continue
        # An Action never writes its in_progress_state (change_state skips
        # the state machinery), so it cannot have stranded an instance
        # there — and Action.fail_transition neither unlocks nor writes
        # failed_state while the state is locked, which would leak the
        # sweep's lock until LOCK_TIMEOUT.
        if isinstance(transition, Action):
            continue
        key = (binding.model._meta.label, binding.state_field,
               in_progress, transition.action_name)
        if key in seen:
            continue
        seen.add(key)
        # Contained per transition: one misbehaving model/binding must
        # not abort the sweep for every remaining binding.
        try:
            recovered += _sweep_transition(binding, transition)
        except Exception:
            logger.exception(
                f'recover_stranded_states: sweep failed for '
                f'{binding.model._meta.label}.{binding.state_field} '
                f'({transition.action_name}); continuing.'
            )
    return recovered


#: Chunk size for the batched uncompleted-TransitionMessage pre-check —
#: bounds the ``instance_id__in`` list (SQLite caps query variables, and a
#: no-broker misconfiguration can pile thousands of rows into an
#: in-progress state).
_TM_SCAN_CHUNK = 500


def _sweep_transition(binding, transition) -> int:
    """Scan one (model, state_field, transition) for stranded instances
    and recover each; returns the number recovered."""
    from django_logic.background.models import TransitionMessage

    # _base_manager, like State.get_persisted_state: a filtered or
    # renamed default manager must not hide stranded rows (or crash
    # the sweep on a model without `.objects`).
    pks = list(
        binding.model._base_manager
        .filter(**{binding.state_field: transition.in_progress_state})
        .values_list('pk', flat=True)
    )
    recovered = 0
    process_name = binding.process_class.process_name
    for start in range(0, len(pks), _TM_SCAN_CHUNK):
        chunk = pks[start:start + _TM_SCAN_CHUNK]
        # One query per chunk: an uncompleted message for THIS process
        # belongs to the background machinery (starter/watchdog), not to
        # this sweep. Scoped by process_name — same as
        # _ensure_no_background_in_flight and the partial unique
        # constraint — so a sibling process's in-flight row cannot delay
        # recovery. Re-checked under the lock before acting.
        in_flight = set(
            TransitionMessage.objects.filter(
                app_label=binding.model._meta.app_label,
                model_name=binding.model._meta.model_name,
                instance_id__in=[str(pk) for pk in chunk],
                process_name=process_name,
                is_completed=False,
            ).values_list('instance_id', flat=True)
        )
        for pk in chunk:
            if str(pk) in in_flight:
                continue
            if _recover_stranded_instance(binding, transition, pk):
                recovered += 1
    return recovered


def _recover_stranded_instance(binding, transition, pk) -> bool:
    """Recover one candidate **under the state lock**, mirroring the
    phase-2 state guard:

    1. ``state.lock()`` — atomic take-ownership, using the bound
       process's declared ``state_class`` (a ``RedisState`` stores the
       *state value* under the lock key, so concurrent readers keep
       seeing a truthful state for the whole recovery window). A live
       execution holds the lock for its whole run, so a failed
       ``lock()`` means "not stranded (or another sweep got here
       first)": skip.
    2. Re-check for an uncompleted ``TransitionMessage`` for this
       process under the lock. Phase 1 must hold this same lock to
       create one, so no new same-process message can appear after
       this check.
    3. Re-read the **persisted** state under the lock, AFTER the message
       check. Order matters: phase-2 completion holds no state lock —
       it commits its state write and ``is_completed`` atomically — so a
       message that completed *between* the two checks has already made
       its state write visible to this later read. A re-drive or a
       manual fix that won the race has moved the state — their write
       wins; release and skip.
    4. Only then drive ``fail_transition``. Lock ownership transfers to
       it — ``Transition.fail_transition``'s ``finally`` releases the
       lock — so the sweep never double-unlocks and can never delete a
       lock a later caller has just acquired. (This contract is why
       ``Action``\\ s — whose ``fail_transition`` holds no lock and
       releases none — are excluded at candidate collection.)

    Returns True when the instance was recovered.
    """
    from django_logic.background.models import TransitionMessage
    from django_logic.logger import logger

    instance = binding.model._base_manager.filter(pk=pk).first()
    if instance is None:
        return False
    state = binding.process_class.state_class(
        instance=instance,
        field_name=binding.state_field,
        process_name=binding.process_class.process_name,
    )
    label = (f'{binding.model._meta.label}#{pk} '
             f'{binding.state_field}={transition.in_progress_state!r} '
             f'({transition.action_name})')

    if not state.lock():
        return False  # live execution (or a concurrent sweep) owns it
    we_hold_lock = True
    try:
        # Message check FIRST (see the docstring for why the order is
        # load-bearing): creation is gated on the lock we hold, and a
        # completion landing before this check has already committed its
        # state write, which the persisted read below then observes.
        if TransitionMessage.objects.filter(
            app_label=instance._meta.app_label,
            model_name=instance._meta.model_name,
            instance_id=str(instance.pk),
            process_name=binding.process_class.process_name,
            is_completed=False,
        ).exists():
            return False  # same-process background work — starter's job
        # get_persisted_state, not get_db_state: the authoritative DB read
        # (RedisState overrides get_db_state with a cached read — under
        # the sweep's own lock that cache entry is the lock payload).
        if state.get_persisted_state() != transition.in_progress_state:
            return False  # moved under us — the other writer wins

        if not transition.failed_state:
            logger.warning(
                f'recover_stranded_states: {label} is stranded but the '
                f'transition declares no failed_state — left as-is '
                f'(re-drivable via the implicit in-progress source).'
            )
            return False

        # Ownership transfer: fail_transition ALWAYS releases the lock in
        # its own finally (even when a hook raises), so from here on the
        # sweep must not unlock again.
        we_hold_lock = False
        transition.fail_transition(
            state,
            RuntimeError(
                f'{STRANDED_MARKER} recovered by recover_stranded_states: '
                f'the process died mid-transition and left this instance '
                f'in {transition.in_progress_state!r}.'
            ),
            tr_id='stranded-recovery',
            # Every other fail_transition caller guarantees `context` to
            # hooks (_init_transition_context / the phase-2 restore);
            # hooks declared `def fn(instance, context, **kwargs)` would
            # otherwise TypeError — swallowed by the hook runners, i.e.
            # silently skipped.
            context={},
        )
        logger.error(
            f'recover_stranded_states: recovered {label} -> '
            f'{transition.failed_state!r}'
        )
        return True
    except Exception as e:
        logger.error(f'recover_stranded_states: failed to recover {label}: {e}')
        return False
    finally:
        if we_hold_lock:
            state.unlock()
