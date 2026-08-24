"""BackgroundTransition / BackgroundAction — durable, queue-routed background work.

``change_state`` enqueues the work (same steps in Pull and Sync mode):

* validate conditions + permissions,
* acquire the state lock for the critical section and revalidate the
  persisted state under it,
* atomically write ``in_progress_state`` (for ``BackgroundTransition``)
  and create a ``TransitionMessage`` row,
* release the lock — from here on the uncompleted ``TransitionMessage``
  row is what gates concurrent transitions,
* hand off to the dispatcher, which notifies the pull workers after
  commit (Pull mode) or executes the worker path inline (Sync mode).

The worker path lives in :mod:`django_logic.background.runner` and is
shared between both modes.
"""
from __future__ import annotations

from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction

from django_logic.background import settings as bg_settings
from django_logic.background.exceptions import AlreadyInProgress, SourceStateChanged
from django_logic.background.models import TransitionMessage
from django_logic.background.serializers import (
    KwargsSerializationError,
    serialize_kwargs,
)
from django_logic.exceptions import TransitionNotAllowed
from django_logic.logger import (
    redact_log_kwargs,
    transition_logger,
    TransitionEventType,
)
from django_logic.state import State
from django_logic.transition import Transition, _refuse_engine_param_kwargs


class BackgroundTransition(Transition):
    """State-changing transition that runs its side-effects on a worker process.

    Optional:
        - ``queue`` — the queue name this transition's row carries.
          Defaults to ``DJANGO_LOGIC['DEFAULT_QUEUE']``
          (``'django_logic'``). Name queues per SLA (e.g. ``critical`` /
          ``slow``) and give each its own ``dl_worker`` process to manage
          performance per queue.
        - ``no_retry_on`` — exception types whose failures are permanent
          for this transition. When a side-effect raises one, the worker
          takes the terminal path on that attempt instead of retrying:
          it writes ``failed_state`` (when declared), marks the row
          completed, and runs ``failure_callbacks``. Use it for
          exception types you do not control; for your own code, raise
          :class:`django_logic.background.exceptions.PermanentFailure`,
          which needs no declaration.

    Recommended:
        - ``in_progress_state`` — if omitted, the state field does not
          change until the worker finishes. Providing it is strongly
          recommended so concurrent readers see "in progress" rather
          than the pre-transition state. Transitions may share one
          freely: it is written atomically with the ``TransitionMessage``
          row, which names the exact transition, so nothing has to infer
          an owner from the state value.
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
        no_retry_on: tuple = (),
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
        no_retry_on = tuple(
            no_retry_on if isinstance(no_retry_on, (list, tuple)) else (no_retry_on,)
        )
        for exception_type in no_retry_on:
            if not (isinstance(exception_type, type)
                    and issubclass(exception_type, BaseException)):
                raise ImproperlyConfigured(
                    f"BackgroundTransition '{action_name}': no_retry_on must "
                    f"contain exception types, got {exception_type!r}."
                )
        self.queue = queue
        self.timeout = timeout
        self.no_retry_on = no_retry_on
        super().__init__(
            action_name=action_name, sources=sources, target=target, **kwargs
        )

    def get_queue_name(self) -> str:
        """The queue name this transition's row carries.

        Resolved lazily (not at class-definition time) so the declared
        ``queue=`` and the ``DEFAULT_QUEUE`` setting are read when the
        transition actually runs.
        """
        return self.queue or bg_settings.default_queue()

    def change_state(self, state: State, **kwargs) -> UUID | None:
        # Before the lock, and before the kwargs are serialized into a row
        # the worker would fail on for the same reason.
        _refuse_engine_param_kwargs(self.action_name, kwargs)
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

        # The resolver already ran the conditions and permissions; the
        # synchronous path does not run them again, and neither does this
        # one. The guard that matters runs under the lock below:
        # _ensure_db_state_in_sources.

        # The cache lock guards only this critical section (validate →
        # create the TransitionMessage → write in_progress_state). It is
        # released in the finally below; from then on the uncompleted
        # TransitionMessage row is what gates concurrent transitions.
        # Holding the cache lock for the whole worker run would leak it if
        # a caller's surrounding transaction rolled back (a cache write
        # does not roll back with the database), and a DB row needs no TTL
        # refresh across long retries.
        if not state.lock():
            # Logged before the raise, at INFO, with the instance key — a
            # frozen instance must not read as "the transition starts and
            # the worker drops it".
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.LOCK.value} '
                f'failed {state.instance_key} — state is locked'
            )
            raise TransitionNotAllowed("State is locked")
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.LOCK.value} '
            f'{state.instance_key}'
        )
        try:
            # Same under-the-lock revalidation as the synchronous path:
            # the source check ran before the lock was acquired.
            self._ensure_db_state_in_sources(state)
            try:
                transition_message = self._enqueue_atomic(
                    state, kwargs, queue_name)
            except AlreadyInProgress:
                # Nothing is retrying a stranded row, so "try again shortly"
                # would be wrong forever. Queried here, after
                # _enqueue_atomic's rolled-back atomic, so the connection is
                # healthy; still under the cache lock. A row that completed
                # in the window keeps AlreadyInProgress — it just finished,
                # so retrying is exactly right.
                if TransitionMessage.retry_status(
                    state.instance, state.process_name,
                ) == TransitionMessage.STRANDED:
                    raise TransitionNotAllowed(
                        f"BackgroundTransition '{self.action_name}' is not "
                        f"allowed: an uncompleted TransitionMessage for "
                        f"{state.instance_key} is stranded — nothing is "
                        f"retrying it. Likely causes: no worker serves its "
                        f"queue, or a worker outage longer than the retry "
                        f"window. Start a worker for that queue "
                        f"(dl_worker --queues ...) — it takes the row at "
                        f"once — or complete the row."
                    )
                raise
        finally:
            state.unlock()
            transition_logger.info(
                f'{kwargs.get("tr_id")} {TransitionEventType.UNLOCK.value} '
                f'{state.instance_key}'
            )

        from django_logic.background.dispatch import dispatch_transition
        dispatch_transition(transition_message)

        return kwargs.get('tr_id')

    def _enqueue_atomic(
        self, state: State, kwargs: dict, queue_name: str
    ) -> TransitionMessage:
        """Atomic: set in_progress_state + create TransitionMessage row.

        Raises :class:`AlreadyInProgress` if the partial unique
        constraint fires (another uncompleted TransitionMessage exists
        for the same instance + process).
        """
        instance_lookup = {
            'app_label': state.instance._meta.app_label,
            'model_name': state.instance._meta.model_name,
            # Models use different primary-key types (int, UUID, string).
            # Store the key as text; _restore looks it up with get(pk=...).
            'instance_id': str(state.instance.pk),
        }
        try:
            serialized = serialize_kwargs(kwargs)
        except KwargsSerializationError:
            # Re-raise so the precise strict-mode message is not wrapped
            # as "not JSON-serializable".
            raise
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
                transition_message = TransitionMessage.objects.create(
                    process_name=state.process_name,
                    # Recorded so the worker can reconstruct the process
                    # from the stored process_class even when the model
                    # property was renamed or rebound in between.
                    field_name=state.field_name,
                    transition_name=self.action_name,
                    # The (possibly nested) process class that declared
                    # this transition, resolved by
                    # Process._get_transition_method. Lets the worker pick
                    # the exact transition when an action_name is shared
                    # across nested processes that use conditions to
                    # choose. Empty when invoked outside that path (e.g. a
                    # directly-constructed transition) — the worker then
                    # falls back to first-match by transition_name.
                    owning_process_class=kwargs.get('owning_process_class', ''),
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
            # the insert can block while a concurrent worker finishes and
            # flips is_completed (the row then leaves the partial unique
            # index). We are admitted after our under-the-lock
            # revalidation, against an instance that worker has already
            # moved to its target or failed state. Without this recheck,
            # two concurrent enqueues on one instance can both be accepted
            # and the transition silently re-runs from a non-source state
            # (reproduced under real worker concurrency).
            current = state.get_persisted_state()
            if current not in self.sources:
                # The atomic block rolls the TransitionMessage row back.
                raise SourceStateChanged(
                    f"BackgroundTransition '{self.action_name}' is not "
                    f"allowed: the persisted state moved to {current!r} "
                    f"while the insert waited on the unique constraint — "
                    f"it is no longer one of the source states."
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
            f'{kwargs.get("tr_id")} TransitionMessage#{transition_message.pk} '
            f'created (queue={queue_name})'
        )
        return transition_message


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
        # never calls this: enqueue stops at the TransitionMessage row and
        # the worker writes state / runs hooks itself (_handle_success /
        # _run_success_hooks). The inherited implementation would write an
        # empty target state; an action must not change state on success.
        pass
