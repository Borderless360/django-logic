"""Command objects wrapped around transition hook lists.

Every hook slot on a ``Transition`` — conditions, permissions,
side-effects, callbacks, failure callbacks — is represented by a
``BaseCommand`` subclass that owns a list of callables and knows how to
run them.
"""
import logging

from django.db import DEFAULT_DB_ALIAS, DatabaseError, transaction

from django_logic.exceptions import RefusalReason, TransitionTemporarilyUnavailable
from django_logic.logger import (
    _running_job_attempt,
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


class _SilentRollback(Exception):
    """The required work rolled back after its function suppressed a database error."""


def _run_in_savepoint(using: str, fn):
    """Run required work in a savepoint and refuse an unreported rollback."""
    connection = transaction.get_connection(using)
    with transaction.atomic(using=using):
        result = fn()
        rolled_back = bool(connection.needs_rollback)
    if rolled_back:
        raise _SilentRollback(
            'A function suppressed a database error. Its savepoint rolled back, '
            'so none of its writes were saved.'
        )
    return result


def _run_keeping_writes(using: str, fn):
    """Run hooks whose caller holds an open transaction, and keep what they saved.

    A database error aborts that transaction. The savepoint discards the
    hooks' writes and keeps the connection usable, so the failed state and
    the failure callbacks can still be written. Any other error keeps the
    writes made so far, as it does for a caller outside a transaction.
    A nested transition that failed keeps its failed state this way.
    """
    connection = transaction.get_connection(using)
    error = None
    with transaction.atomic(using=using):
        try:
            result = fn()
        except Exception as exc:
            if isinstance(exc, DatabaseError) or connection.needs_rollback:
                raise
            error = exc
        if error is None and connection.needs_rollback:
            raise _SilentRollback(
                'a hook raised a database error and suppressed it, so '
                'every write the hooks made is discarded.'
            )
    if error is not None:
        raise error
    return result


def write_failed_state(state, failed_state, *, prefix, consequence, using=None):
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
    A savepoint Django discards without an exception is still a failure;
    only a successful write can produce a ``SET_STATE`` log entry.
    """
    database_kwargs = {'using': using} if using is not None else {}
    previous = state.get_state()
    try:
        _run_in_savepoint(
            using or state.instance._state.db or DEFAULT_DB_ALIAS,
            lambda: state.set_state(failed_state, **database_kwargs),
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
    # An error tracker groups and filters the line by the exception it carries.
    log_kwargs.setdefault('exc_info', error)
    transition_logger.log(level, message, **log_kwargs)


def reload_after_rollback(state, *, prefix):
    """Do not pass discarded model values to failure callbacks."""
    using = state.instance._state.db or DEFAULT_DB_ALIAS
    try:
        _run_in_savepoint(
            using, lambda: state.instance.refresh_from_db(using=using),
        )
    except Exception as refresh_error:
        _log_hook_error(
            f'{prefix} could not reload {state.instance_key} after rollback; '
            'skipping further failure writes and callbacks. The original '
            'required-work error is re-raised. Reload error: '
            f'{type(refresh_error).__name__}: {refresh_error}',
            refresh_error,
        )
        return False
    return True


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


def refusal_message(text):
    """Attach a literal refusal detail to a condition or permission callable."""
    def annotate(command):
        command._django_logic_refusal_message = text
        return command
    return annotate


class Conditions(BaseCommand):
    def execute(self, instance, *args, **kwargs):
        for check in self.commands:
            if not check(instance, *args, **kwargs):
                return check
        return None


class Permissions(Conditions):
    def execute(self, instance, user, **kwargs):
        if user is None:
            return None
        return super().execute(instance, user, **kwargs)


class StrictPermissions(Permissions):
    """Allow only a person who passes the declared permissions."""

    def execute(self, instance, user, **kwargs):
        if not user:
            return self
        return super().execute(instance, user, **kwargs)


class NoUserPermissions(Permissions):
    """Allow only a system call."""

    def execute(self, instance, user, **kwargs):
        if user:
            return self
        return None


def guard_failure(permissions, conditions, instance, user):
    failed = permissions.execute(instance, user)
    if failed is not None:
        return RefusalReason.PERMISSION, getattr(failed, '_django_logic_refusal_message', None)
    failed = conditions.execute(instance)
    if failed is not None:
        return RefusalReason.CONDITION, getattr(failed, '_django_logic_refusal_message', None)
    return None


class SideEffects(BaseCommand):
    """Essential work for a transition.

    On exception, the transition's ``fail_transition`` is invoked and the
    exception is re-raised so callers can observe the failure.
    """

    def execute(self, state: State, **kwargs):
        command_name = 'before the first side effect'
        def run_commands():
            nonlocal command_name
            for command in self.commands:
                command_name = type(command).__name__
                command_name = getattr(command, '__name__', None) or command_name
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.SIDE_EFFECT.value} '
                    f'{command_name}'
                )
                command(state.instance, **kwargs)

        def run_required_changes():
            nonlocal command_name
            run_commands()
            command_name = 'transition completion'
            self._transition.complete_transition(state, **kwargs)

        try:
            transition_logger.info(
                f'{kwargs.get("tr_id")} SideEffects {len(self.commands)}'
            )
            using = state.instance._state.db or DEFAULT_DB_ALIAS
            _run_in_savepoint(using, run_required_changes)
        except Exception as error:
            _log_hook_error(
                f'{kwargs.get("tr_id")} {TransitionEventType.SIDE_EFFECT.value} '
                f'{command_name} failed action={self._transition.action_name} '
                f'instance={state.instance_key}: {type(error).__name__}: {error}',
                error,
                extra={'job_failure': 'attempt'} if _running_job_attempt.get() else {},
            )
            raise


class Callbacks(BaseCommand):
    """Best-effort follow-ups. Exceptions are logged and swallowed.

    Each callback is isolated: one failing callback does not
    prevent the later ones from being attempted, and when the caller is
    inside an open transaction each callback runs in its own savepoint —
    a database error then discards only that callback's writes and leaves
    the outer transaction usable.
    Any other error keeps what the callback saved before it failed, inside
    a transaction and outside one. A transition that a callback starts
    keeps its failed state when it fails.
    """

    def after_commit(self, state, using, kwargs):
        from django_logic.conf import sync_enabled
        from django_logic.process import _transition_context

        def run():
            token = _transition_context.set({'root_id': kwargs.get('root_id'), 'tr_id': kwargs.get('tr_id')})
            try:
                self.execute(state, **kwargs)
            finally:
                _transition_context.reset(token)

        blocks = transaction.get_connection(using).atomic_blocks
        if sync_enabled() and blocks and all(getattr(block, '_from_testcase', False) for block in blocks):
            # Django TestCase never commits its wrapper. An application atomic block still controls delivery.
            run()
        else:
            transaction.on_commit(run, using=using)

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
                    _run_keeping_writes(
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
