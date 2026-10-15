"""Run provider work while preserving each committed model write.

The job row stays locked on a separate connection. Model work uses autocommit.
The final state and job completion are written in the same transaction.
"""
from contextlib import contextmanager
from uuid import uuid4

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, connections, router, transaction

from django_logic.background.models import TransitionMessage
from django_logic.commands import _run_in_savepoint
from django_logic.logger import TransitionEventType, transition_logger
from django_logic.process import _transition_context


def validate_non_atomic(instance, *, require_autocommit):
    """Refuse database arrangements that cannot preserve independent writes."""
    model = type(instance)
    aliases = {
        instance._state.db or DEFAULT_DB_ALIAS,
        router.db_for_read(model),
        router.db_for_write(model, instance=instance),
        router.db_for_read(TransitionMessage),
        router.db_for_write(TransitionMessage),
    }
    connection = connections[DEFAULT_DB_ALIAS]
    if aliases != {DEFAULT_DB_ALIAS} or connection.vendor != 'postgresql':
        raise ImproperlyConfigured(
            'BackgroundTransition(atomic=False) requires the model and job to use '
            'the default PostgreSQL database for reads and writes.'
        )
    if require_autocommit and not connection.get_autocommit():
        raise ImproperlyConfigured(
            'BackgroundTransition(atomic=False) cannot execute inside a caller transaction. '
            'Use pull execution, or run inline with autocommit enabled.'
        )


@contextmanager
def _job_connection():
    """Create one temporary connection for this job, and always close it."""
    alias = f'_django_logic_job_{uuid4().hex}'
    connection = connections[DEFAULT_DB_ALIAS].copy(alias=alias)
    # A temporary alias must not create a Django connection pool that outlives the job.
    connection.settings_dict['OPTIONS'].pop('pool', None)
    connections[alias] = connection
    try:
        yield alias
    finally:
        connection.close()
        del connections[alias]


def run_non_atomic(transition_message_id):
    """Keep provider receipts when a later operation or final state write fails."""
    from django_logic.background.runner import (
        _decode_kwargs,
        _handle_failure,
        _handle_restore_failure,
        _handle_success,
        _lock_uncompleted_row,
        _restore_for_attempt,
        _superseded_outcome,
    )

    if not connections[DEFAULT_DB_ALIAS].get_autocommit():
        raise ImproperlyConfigured(
            'BackgroundTransition(atomic=False) cannot execute inside a caller transaction.'
        )
    with _job_connection() as alias, transaction.atomic(using=alias):
        transition_message = _lock_uncompleted_row(transition_message_id, using=alias)
        kwargs, decode_error = _decode_kwargs(transition_message)
        restored, restore_error = _restore_for_attempt(transition_message)
        if restore_error is not None:
            return _handle_restore_failure(transition_message, restore_error)
        instance, process, transition = restored
        state = process.state
        try:
            validate_non_atomic(instance, require_autocommit=True)
        except ImproperlyConfigured as error:
            return _handle_restore_failure(transition_message, error)
        superseded = _superseded_outcome(transition_message, transition, state, kwargs)
        if superseded is not None:
            return superseded
        token = _transition_context.set({
            'root_id': kwargs.get('root_id'),
            'tr_id': kwargs.get('tr_id'),
        })
        original_alias = instance._state.db
        try:
            transition_logger.info(
                f'{kwargs.get("tr_id")} Execute Start {transition.action_name} '
                f'{state.instance_key} queue={transition_message.queue_name}'
            )
            error = decode_error
            if error is None:
                try:
                    for command in transition.side_effects.commands:
                        # Refuse another operation if this connection lost its job lock.
                        # An external request already in progress still needs a saved receipt.
                        with connections[alias].cursor() as cursor:
                            cursor.execute('SELECT 1')
                        transition_logger.info(
                            f'{kwargs.get("tr_id")} {TransitionEventType.SIDE_EFFECT.value} '
                            f'{getattr(command, "__name__", repr(command))}'
                        )
                        command(instance, **kwargs)
                except Exception as exception:
                    error = exception

            # The state guard and final writes use the connection that still holds the job.
            # Provider writes used the normal connection and have already committed.
            instance._state.db = alias
            superseded = _superseded_outcome(
                transition_message, transition, state, kwargs, after_side_effects=True,
            )
            if superseded is not None:
                return superseded
            if error is not None:
                return _handle_failure(
                    transition_message, transition, state, kwargs, error, using=alias,
                )

            def complete():
                if transition.target is not None:
                    state.set_state(transition.target, using=alias)
                    transition_logger.info(
                        f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
                        f'{transition.target}'
                    )
                return _handle_success(transition_message, transition, state, kwargs)

            try:
                return _run_in_savepoint(alias, complete, require_commit=True)
            except Exception as exception:
                instance.refresh_from_db(using=alias, fields=[state.field_name])
                return _handle_failure(
                    transition_message, transition, state, kwargs, exception, using=alias,
                )
        finally:
            instance._state.db = original_alias
            _transition_context.reset(token)
