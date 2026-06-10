"""BackgroundTransition / BackgroundAction — durable, queue-routed Celery tasks.

Phase 1 (what ``change_state`` does) is identical in both Celery and
Sync execution modes:

* validate conditions + permissions,
* acquire the state lock for the critical section and revalidate the
  persisted state under it,
* atomically write ``in_progress_state`` (for ``BackgroundTransition``)
  and create a ``TransitionMessage`` row,
* release the lock — from here on the uncompleted ``TransitionMessage``
  row is the durable in-flight marker that gates concurrent transitions,
* hand the row id to the dispatcher, which either ``apply_async`` the
  Celery task (Celery mode) or call phase 2 inline (Sync mode).

Phase 2 lives in :mod:`django_logic.background.runner` and is shared
between both modes.
"""
from __future__ import annotations

from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction

from django_logic.background import settings as bg_settings
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from django_logic.background.serializers import serialize_kwargs
from django_logic.exceptions import TransitionNotAllowed
from django_logic.logger import (
    redact_log_kwargs,
    transition_logger,
    TransitionEventType,
)
from django_logic.state import State
from django_logic.transition import Transition


class BackgroundTransition(Transition):
    """State-changing transition that runs its side-effects on a Celery worker.

    Optional:
        - ``queue`` — the Celery queue this transition's task is routed
          to. Defaults to ``DJANGO_LOGIC['DEFAULT_QUEUE']``
          (``'django_logic'``). Name queues per SLA (e.g. ``critical`` /
          ``slow``) and give each its own worker to manage performance
          per queue.

    Recommended:
        - ``in_progress_state`` — if omitted, the state field does not
          change until phase 2 finishes. Providing it is strongly
          recommended so concurrent readers see "in progress" rather
          than the pre-transition state. ``in_progress_state`` values
          must be unique within a :class:`django_logic.Process`.
    """

    is_background = True

    def __init__(
        self,
        action_name: str,
        sources: list,
        target: str,
        *,
        queue: str | None = None,
        timeout: int | None = None,
        **kwargs,
    ):
        if queue is not None and (not queue or not isinstance(queue, str)):
            raise ImproperlyConfigured(
                f"BackgroundTransition '{action_name}': queue must be a "
                f"non-empty string when provided (omit it to use "
                f"DJANGO_LOGIC['DEFAULT_QUEUE'])."
            )
        if timeout is not None:
            if not isinstance(timeout, int) or timeout <= 0:
                raise ImproperlyConfigured(
                    f"BackgroundTransition '{action_name}': timeout must "
                    f"be a positive integer number of seconds, got "
                    f"{timeout!r}."
                )
        self.queue = queue
        self.timeout = timeout
        super().__init__(
            action_name=action_name, sources=sources, target=target, **kwargs
        )

    def get_queue_name(self) -> str:
        """The Celery queue this transition's task is routed to.

        Resolved lazily (not at class-definition time) so the declared
        ``queue=`` and the ``DEFAULT_QUEUE`` setting are read when the
        transition actually runs.
        """
        return self.queue or bg_settings.default_queue()

    def change_state(self, state: State, **kwargs) -> UUID | None:
        process_class = kwargs.get('process_class', '')
        process_class_name = process_class.split('.')[-1] if process_class else ''
        queue_name = self.get_queue_name()
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.START.value} '
            f'{process_class_name} {self.action_name} {state.instance_key} '
            f'{kwargs.get("root_id")} {kwargs.get("parent_id")} '
            f'[background queue={queue_name}]',
            extra={'kwargs': redact_log_kwargs(kwargs), 'state_hash': state._get_hash()},
        )

        if not self.is_valid(state.instance, kwargs.get('user')):
            raise TransitionNotAllowed(
                f"BackgroundTransition '{self.action_name}' rejected by "
                f"its conditions or permissions."
            )

        # The cache lock guards only this critical section (validate →
        # create the TransitionMessage → write in_progress_state). It is
        # released in the finally below; from then on the uncompleted
        # TransitionMessage row is the durable in-flight marker. Holding
        # the cache lock for the whole background flight would leak it if
        # a caller's surrounding transaction rolled back (a cache write
        # does not roll back with the database), and a DB row needs no TTL
        # refresh across long retries.
        if not state.lock():
            raise TransitionNotAllowed("State is locked")
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.LOCK.value}'
        )
        try:
            # Same under-the-lock revalidation as the synchronous path:
            # the source check ran before the lock was acquired.
            self._ensure_db_state_in_sources(state)
            tm = self._phase_one_atomic(state, kwargs, queue_name)
        finally:
            state.unlock()
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.UNLOCK.value}'
            )

        from django_logic.background.dispatch import dispatch_transition
        dispatch_transition(tm)

        return kwargs.get('tr_id')

    def _phase_one_atomic(
        self, state: State, kwargs: dict, queue_name: str
    ) -> TransitionMessage:
        """Atomic: set in_progress_state + create TransitionMessage row.

        Raises :class:`AlreadyInProgress` if the partial unique
        constraint fires (another uncompleted TM exists for the same
        instance + process).
        """
        instance_lookup = {
            'app_label': state.instance._meta.app_label,
            'model_name': state.instance._meta.model_name,
            # str() so UUID / CharField / big-int PKs all round-trip through
            # the TextField; _restore coerces it back via get(pk=...).
            'instance_id': str(state.instance.pk),
        }
        try:
            serialized = serialize_kwargs(kwargs)
        except TypeError as e:
            raise ImproperlyConfigured(
                f"BackgroundTransition '{self.action_name}' received a "
                f"kwarg that is not JSON-serializable: {e}. Every value "
                f"passed to a background transition must be persistable "
                f"on the TransitionMessage row."
            ) from e

        with transaction.atomic():
            # Create the TransitionMessage FIRST. It carries the partial
            # unique constraint and has no other unique/FK constraints, so
            # an IntegrityError from this create is unambiguously the
            # concurrency guard firing. Writing in_progress_state first
            # instead would let a model-level constraint on the state
            # column (CHECK, NOT NULL, FK, trigger) surface as the
            # misleading "another transition is already in progress".
            try:
                tm = TransitionMessage.objects.create(
                    process_name=state.process_name,
                    # Recorded so phase 2 can reconstruct the process from
                    # the stored process_class even when the model property
                    # was renamed/rebound between phases.
                    field_name=state.field_name,
                    transition_name=self.action_name,
                    queue_name=queue_name,
                    timeout_seconds=self.timeout,
                    kwargs=serialized,
                    **instance_lookup,
                )
            except IntegrityError as exc:
                raise AlreadyInProgress(
                    f"{state.instance_key}: another background transition "
                    f"is already in progress for this instance and process "
                    f"'{state.process_name}'."
                ) from exc

            # Recheck the persisted state AFTER the create. On PostgreSQL
            # the insert can block in a speculative-insert wait while a
            # concurrent flight's phase 2 finishes (its row leaves the
            # partial unique index the moment is_completed flips) — we are
            # then admitted seconds after our under-the-lock revalidation,
            # against an instance the finished flight has already moved to
            # its target/failed state. Without this recheck, two concurrent
            # phase 1s on one instance can BOTH be accepted and the
            # transition silently re-runs from a non-source state
            # (reproduced under real worker concurrency).
            current = state.get_persisted_state()
            if current not in self.sources:
                # The atomic block rolls the TM row back.
                raise TransitionNotAllowed(
                    f"BackgroundTransition '{self.action_name}' is not "
                    f"allowed: the persisted state moved to {current!r} "
                    f"while phase 1 waited on a finishing flight — it is "
                    f"no longer one of the source states."
                )

            if self.in_progress_state:
                # A constraint violation here propagates as a raw
                # IntegrityError (not AlreadyInProgress) — it is the user's
                # own model constraint, not our concurrency guard.
                state.set_state(self.in_progress_state)
                transition_logger.info(
                    f'{kwargs.get("tr_id")} '
                    f'{TransitionEventType.SET_STATE.value} '
                    f'{self.in_progress_state}'
                )

        transition_logger.info(
            f'{kwargs.get("tr_id")} TransitionMessage#{tm.pk} created '
            f'(queue={queue_name})'
        )
        return tm


class BackgroundAction(BackgroundTransition):
    """Background-executed action — runs side-effects with no state change.

    Same durability contract as :class:`BackgroundTransition`. The only
    differences:

    * ``target`` is always empty (no state write on success),
    * ``in_progress_state`` is not meaningful and is rejected at
      construction time,
    * failure at ``MAX_ERRORS`` optionally writes ``failed_state``.
    """

    def __init__(
        self, action_name: str, sources: list, *, queue: str | None = None, **kwargs
    ):
        if kwargs.get('in_progress_state'):
            raise ImproperlyConfigured(
                f"BackgroundAction '{action_name}' cannot declare "
                f"in_progress_state — actions do not change state on "
                f"success. Use BackgroundTransition if you need to mark "
                f"in-progress."
            )
        # target='' is the sentinel for "no state change".
        super().__init__(
            action_name=action_name,
            sources=sources,
            target='',
            queue=queue,
            **kwargs,
        )

    def __str__(self) -> str:
        return f"BackgroundAction: {self.action_name}"

    def complete_transition(self, state: State, **kwargs):
        # Defensive no-op for direct/manual invocation only — the engine
        # never calls this: phase 1 stops at the TransitionMessage row and
        # phase 2 writes state / runs hooks itself (_handle_success /
        # _run_success_hooks). The inherited implementation would write an
        # empty target state; an action must not change state on success.
        pass
