"""Command objects wrapped around transition hook lists.

Every hook slot on a ``Transition`` — conditions, permissions,
side-effects, callbacks, failure callbacks — is represented by a
``BaseCommand`` subclass that owns a list of callables and knows how to
run them.
"""
import logging

from django.db import DEFAULT_DB_ALIAS, transaction

from django_logic.exceptions import TransitionTemporarilyUnavailable
from django_logic.logger import (
    transition_logger,
    TransitionEventType,
)
from django_logic.state import State


def _in_open_transaction(instance) -> tuple[str, bool]:
    """The instance's DB alias, and whether that connection is inside an
    open ``atomic`` block (savepoint isolation is only needed — and only
    safe to add without changing autocommit semantics — in that case)."""
    using = instance._state.db or DEFAULT_DB_ALIAS
    return using, transaction.get_connection(using).in_atomic_block


def _deferred_unlocks(conn) -> list:
    if not hasattr(conn, '_dl_deferred_unlocks'):
        conn._dl_deferred_unlocks = []
    return conn._dl_deferred_unlocks


def note_deferred_unlock(using: str, state: State) -> None:
    """Record a DEFER_UNLOCK_UNTIL_COMMIT unlock so hook
    savepoints can release it if their rollback discards the
    ``transaction.on_commit`` registration (see ``_run_in_savepoint``).
    Called by ``Transition._release_lock`` right after registering the
    on_commit hook."""
    conn = transaction.get_connection(using)
    registry = _deferred_unlocks(conn)
    # Re-register the clear unless our hook is still queued — ask Django,
    # not the registry: a rollback discards the hook but leaves the
    # entries, so a registry-emptiness key never re-registers and the
    # list pins every State for the life of the connection.
    queued = any(
        any(getattr(item, '_dl_deferred_clear', False)
            for item in entry if callable(item))
        for entry in getattr(conn, 'run_on_commit', ()) or ()
    )
    if not queued:
        # Re-register only. Do NOT clear here, however stale the entries look:
        # ``_run_in_savepoint`` tracks its own entries by INDEX WINDOW
        # (``before = len(registry)`` … ``registry[before:]``), so clearing
        # mid-transaction shifts those indices and would drop deferred unlocks
        # an enclosing window is still responsible for releasing — leaking the
        # exact locks this registry exists to release. The hook registered
        # below drains everything at the next successful commit, which is
        # bounded and cannot interleave with an active window.
        def _clear():
            registry.clear()

        _clear._dl_deferred_clear = True
        transaction.on_commit(_clear, using=using)
    registry.append(state)


class _SilentRollback(Exception):
    """Internal: the savepoint rolled back with NO exception propagating.

    Raised only for callers that pass ``require_commit`` — those whose next
    step reports the work as done. Without it "``fn`` returned" reads as
    "``fn``'s writes committed", which is false on this path: the writes are
    gone and nothing raised, so the caller commits its own bookkeeping on top
    of an attempt that never happened.
    """


def _run_in_savepoint(using: str, fn, *, require_commit: bool = False):
    """Run ``fn`` inside a savepoint, without losing deferred unlocks.

    When the savepoint rolls back, Django discards every
    ``transaction.on_commit`` hook registered inside it — including the
    DEFER_UNLOCK_UNTIL_COMMIT unlocks of transitions the hook
    drove. Their state writes roll back with the savepoint, so those
    locks protect nothing anymore; dropping the hooks would leak them
    until TTL *while the outer transaction commits successfully*.
    On rollback, release exactly the unlocks registered within this
    savepoint's window (``unlock()`` is a token compare-and-delete, so
    this can never race a lock that was legitimately re-acquired).

    ``require_commit`` turns the silent rollback below into a raised
    ``_SilentRollback`` — for callers whose next act is to record the work
    as done."""
    conn = transaction.get_connection(using)
    registry = _deferred_unlocks(conn)
    before = len(registry)
    rolled_back = False
    try:
        with transaction.atomic(using=using):
            result = fn()
            # A savepoint also rolls back with NO exception propagating:
            # Atomic.__exit__ takes the rollback branch on
            # `exc_type is None and connection.needs_rollback`, and
            # needs_rollback is set by mark_for_rollback_on_error — which
            # Model.save_base wraps every write in — whenever a database error
            # is raised inside the block, even if the hook caught it. That
            # silent rollback strips the same on_commit hooks, so the deferred
            # unlocks it discards must be released here too.
            rolled_back = bool(conn.needs_rollback)
    except BaseException:
        rolled_back = True
        _release_dropped(registry, before)
        raise
    if rolled_back:
        _release_dropped(registry, before)
        # Worth a line even where the caller tolerates it (best-effort hook
        # bundles): the writes are gone and nothing raised, so their absence
        # is the only other trace anyone gets.
        note = (
            'a savepoint rolled back with no exception propagating — a '
            'database error inside it was raised and then suppressed, so '
            'every write it made was discarded.'
        )
        transition_logger.warning(note)
        if require_commit:
            raise _SilentRollback(note)
    return result


def write_failed_state(state, failed_state, *, prefix, consequence):
    """Write ``failed_state`` in a savepoint. The one copy of this write.

    Returns ``None`` when the write landed (and logs the ``SET_STATE``
    line), or the write's exception when it did not. On a failed write
    the instance attribute is restored to the pre-write value — a
    discarded savepoint leaves it refreshed to a value the database
    never had, and the failure hooks must not read that. The caller
    decides what a failed write means: the synchronous paths re-raise
    the original side-effect exception unchanged; the worker paths
    record the error on the row and complete it anyway. ``prefix``
    starts both log lines; ``consequence`` tells the operator what
    happens next.

    The savepoint opens on the instance's alias, not DEFAULT: set_state
    routes its write with ``hints={'instance': ...}``, so a savepoint
    opened on DEFAULT would guard the wrong connection.
    ``require_commit``, because the success branch logs ``SET_STATE`` —
    a savepoint Django discards without an exception must surface as
    the failure it is, not log a state change that never landed.
    """
    previous = state.get_state()
    try:
        _run_in_savepoint(
            state.instance._state.db or DEFAULT_DB_ALIAS,
            lambda: state.set_state(failed_state),
            require_commit=True,
        )
    except Exception as write_error:
        setattr(state.instance, state.field_name, previous)
        transition_logger.error(
            f'{prefix} could not write failed_state {failed_state!r} on '
            f'{state.instance_key}: {type(write_error).__name__}: '
            f'{write_error}. {consequence}',
            exc_info=True,
        )
        return write_error
    transition_logger.info(
        f'{prefix} {TransitionEventType.SET_STATE.value} {failed_state}'
    )
    return None


def _release_dropped(registry, before) -> None:
    """Release the deferred unlocks registered inside a rolled-back window."""
    dropped = registry[before:]
    del registry[before:]
    for state in dropped:
        # Each release is contained: one cache blip must not skip the
        # remaining sibling unlocks or replace the hook's original exception
        # (a missed release degrades to the TTL-bounded leak).
        try:
            state.unlock()
        except Exception:
            transition_logger.exception(
                f'failed to release a deferred unlock for '
                f'{state.instance_key} after a savepoint rollback; '
                f'the lock expires via its TTL.'
            )


def _log_hook_error(message: str, error: BaseException, **log_kwargs) -> None:
    """Log a hook failure at ERROR — or WARNING when it is a transient
    concurrency outcome.

    ``TransitionTemporarilyUnavailable`` means "another transition owns
    this instance right now": the designed outcome of two drives racing, and
    the common shape when a background transition is invoked from another
    transition's side-effects. At ERROR it pages an on-call for healthy
    contention. Every other exception stays at ERROR. Consumer subclasses
    of the base inherit the WARNING treatment — the type's contract is
    that it means "retry shortly".
    """
    level = logging.ERROR
    if isinstance(error, TransitionTemporarilyUnavailable):
        level = logging.WARNING
    transition_logger.log(level, message, **log_kwargs)


class BaseCommand:
    """Base class for command bundles (Pattern: Command)."""

    def __init__(self, commands=None, transition=None):
        self._commands = commands or []
        self._transition = transition

    @property
    def commands(self):
        return self._commands

    def execute(self, *args, **kwargs):
        raise NotImplementedError


class Conditions(BaseCommand):
    def execute(self, instance, **kwargs):
        return all(command(instance, **kwargs) for command in self.commands)


class Permissions(BaseCommand):
    def execute(self, instance, user, **kwargs):
        # user=None means "no user context" — treated as permitted.
        # Callers that need authenticated-only transitions must enforce that
        # at the caller site.
        return user is None or all(
            command(instance, user, **kwargs) for command in self.commands
        )


class SideEffects(BaseCommand):
    """Essential work for a transition.

    On exception, the transition's ``fail_transition`` is invoked and the
    exception is re-raised so callers can observe the failure.
    """

    def execute(self, state: State, **kwargs):
        try:
            transition_logger.info(
                f'{kwargs.get("tr_id")} SideEffects {len(self.commands)}'
            )
            for command in self.commands:
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.SIDE_EFFECT.value} '
                    f'{getattr(command, "__name__", repr(command))}'
                )
                command(state.instance, **kwargs)
        except Exception as error:
            _log_hook_error(f'{kwargs.get("tr_id")} {error}', error)
            self._transition.fail_transition(state, error, **kwargs)
            raise
        else:
            self._transition.complete_transition(state, **kwargs)


class Callbacks(BaseCommand):
    """Best-effort follow-ups. Exceptions are logged and swallowed.

    Each callback is isolated: one failing callback does not
    prevent the later ones from being attempted, and when the caller is
    inside an open transaction each callback runs in its own savepoint —
    a database error would otherwise mark the whole outer transaction
    rollback-only, so the swallow left every later ORM call broken
    (``TransactionManagementError``) and could roll back the transition's
    own state write. Outside a transaction there is nothing to poison and
    no savepoint is taken (a failing callback's earlier autocommit writes
    persist, as before).
    """

    def execute(self, state: State, **kwargs):
        transition_logger.info(
            f'{kwargs.get("tr_id")} Callbacks {len(self.commands)}'
        )
        using, in_transaction = _in_open_transaction(state.instance)
        for command in self.commands:
            # object.__repr__ cannot raise for any object (it is just the type
            # name and id), so the except handler below always has a label.
            # The friendlier __name__ lookup happens inside the try, because a
            # pathological __getattr__ can raise — and a label that escaped
            # the swallow contract would replace the original failure
            # exception (Callbacks backs failure_callbacks too).
            command_name = object.__repr__(command)
            try:
                command_name = getattr(command, '__name__', None) or command_name
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.CALLBACK.value} '
                    f'{command_name}'
                )
                if in_transaction:
                    _run_in_savepoint(
                        using, lambda: command(state.instance, **kwargs))
                else:
                    command(state.instance, **kwargs)
            except Exception as error:
                _log_hook_error(
                    f'{kwargs.get("tr_id")} {TransitionEventType.CALLBACK.value} '
                    f'{command_name}: {error}',
                    error,
                    exc_info=True,
                    extra={'kwargs': dict(kwargs)},
                )


class NextTransition:
    """Run a follow-up transition after the current one unlocks.

    A dedicated slot because the follow-up must run in the same call
    frame, after the state unlock: side-effects run before unlock (the
    follow-up would deadlock on its own lock acquisition), and callbacks
    execute on a worker process for background transitions — only for
    synchronous transitions do they run inline.
    """

    def __init__(self, next_transition: str | None = None):
        self._next_transition = next_transition

    def execute(self, state: State, **kwargs):
        if not self._next_transition:
            return

        process = getattr(state.instance, state.process_name)
        transitions = list(
            process.get_available_transitions(
                action_name=self._next_transition,
                user=kwargs.get('user'),
            )
        )
        if not transitions:
            # Not currently available (state/conditions) — skip silently,
            # as a follow-up is best-effort.
            return None
        if len(transitions) > 1:
            # Parity with Process._resolve_transition_with_owner: refuse to
            # guess between ambiguous matches rather than silently running
            # whichever happens to be first in iteration order.
            transition_logger.error(
                f"{kwargs.get('tr_id')} {TransitionEventType.NEXT_TRANSITION.value} "
                f"'{self._next_transition}' is ambiguous "
                f"({len(transitions)} matches); not running any."
            )
            return None

        if getattr(transitions[0], 'is_background', False):
            # request is enqueue-only and unserializable; forwarding it
            # into a background follow-up would fail kwargs serialization
            # under STRICT_KWARGS_SERIALIZATION — and that failure is
            # swallowed below, silently killing the chain.
            kwargs = {k: v for k, v in kwargs.items() if k != 'request'}

        using, in_transaction = _in_open_transaction(state.instance)
        try:
            # Invoke through the Process entrypoint so the follow-up mints
            # its own tr_id and manages _transition_context (root_id chains,
            # parent_id = this transition), instead of inheriting the
            # parent's tr_id via a direct change_state call. Failures of the
            # follow-up must not bubble into the current transition — and,
            # like Callbacks, a swallowed database error inside an open
            # transaction must not poison it, so the follow-up runs
            # in a savepoint there.
            if in_transaction:
                return _run_in_savepoint(
                    using,
                    lambda: getattr(process, self._next_transition)(**kwargs))
            return getattr(process, self._next_transition)(**kwargs)
        except Exception as error:
            _log_hook_error(
                f"{kwargs.get('tr_id')} "
                f"{TransitionEventType.NEXT_TRANSITION.value} "
                f"'{self._next_transition}' failed (swallowed): {error}",
                error,
            )
