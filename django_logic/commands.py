"""Command objects wrapped around transition hook lists.

Every hook slot on a ``Transition`` — conditions, permissions,
side-effects, callbacks, failure side-effects, failure callbacks — is
represented by a ``BaseCommand`` subclass that owns a list of callables
and knows how to run them.
"""
from django_logic.logger import (
    redact_log_kwargs,
    transition_logger,
    TransitionEventType,
)
from django_logic.state import State


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
        return all(command(instance, **kwargs) for command in self._commands)


class Permissions(BaseCommand):
    def execute(self, instance, user, **kwargs):
        # user=None means "no user context" — treated as permitted.
        # Callers that need authenticated-only transitions must enforce that
        # at the caller site.
        return user is None or all(
            command(instance, user, **kwargs) for command in self._commands
        )


class SideEffects(BaseCommand):
    """Essential work for a transition.

    On exception, the transition's ``fail_transition`` is invoked and the
    exception is re-raised so callers can observe the failure.
    """

    def execute(self, state: State, **kwargs):
        try:
            transition_logger.info(
                f'{kwargs.get("tr_id")} SideEffects {len(self._commands)}'
            )
            for command in self._commands:
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.SIDE_EFFECT.value} '
                    f'{getattr(command, "__name__", repr(command))}'
                )
                command(state.instance, **kwargs)
        except Exception as error:
            transition_logger.error(f'{kwargs.get("tr_id")} {error}')
            self._transition.fail_transition(state, error, **kwargs)
            raise
        else:
            self._transition.complete_transition(state, **kwargs)


class Callbacks(BaseCommand):
    """Best-effort follow-ups. Exceptions are logged and swallowed."""

    def execute(self, state: State, **kwargs):
        transition_logger.info(
            f'{kwargs.get("tr_id")} Callbacks {len(self._commands)}'
        )
        command_name = None
        try:
            for command in self.commands:
                command_name = getattr(command, '__name__', repr(command))
                transition_logger.info(
                    f'{kwargs.get("tr_id")} {TransitionEventType.CALLBACK.value} '
                    f'{command_name}'
                )
                command(state.instance, **kwargs)
        except Exception as error:
            transition_logger.error(
                f'{kwargs.get("tr_id")} {TransitionEventType.CALLBACK.value} '
                f'{command_name}: {error}',
                exc_info=True,
                extra={'kwargs': redact_log_kwargs(kwargs)},
            )


class FailureSideEffects(BaseCommand):
    """Runs inside ``fail_transition``, before state unlock.

    Exceptions here are logged and swallowed to avoid masking the original
    failure that triggered ``fail_transition``, but the raised exception
    is returned to the caller so background transitions can record it on
    the ``TransitionMessage`` (otherwise broken cleanup is invisible).
    """

    def execute(self, state: State, **kwargs):
        try:
            transition_logger.info(
                f'{kwargs.get("tr_id")} FailureSideEffects {len(self._commands)}'
            )
            for command in self.commands:
                transition_logger.info(
                    f'{kwargs.get("tr_id")} '
                    f'{TransitionEventType.FAILURE_SIDE_EFFECT.value} '
                    f'{getattr(command, "__name__", repr(command))}'
                )
                command(state.instance, **kwargs)
        except Exception as error:
            transition_logger.error(error)
            return error
        return None


class NextTransition:
    """Run a follow-up transition after the current one unlocks.

    A dedicated slot because the follow-up must run in the same call
    frame, after the state unlock: side-effects run before unlock (the
    follow-up would deadlock on its own lock acquisition), and callbacks
    execute in phase 2 on a Celery worker for background transitions —
    only for synchronous transitions do they run inline.
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
            # Parity with Process.get_transition_by_action_name: refuse to
            # guess between ambiguous matches rather than silently running
            # whichever happens to be first in iteration order.
            transition_logger.error(
                f"{kwargs.get('tr_id')} {TransitionEventType.NEXT_TRANSITION.value} "
                f"'{self._next_transition}' is ambiguous "
                f"({len(transitions)} matches); not running any."
            )
            return None

        if getattr(transitions[0], 'is_background', False):
            # request is phase-1-only and unserializable; forwarding it into
            # a background follow-up would fail phase-1 serialization under
            # STRICT_KWARGS_SERIALIZATION — and that failure is swallowed
            # below, silently killing the chain (#129).
            kwargs = {k: v for k, v in kwargs.items() if k != 'request'}

        try:
            # Invoke through the Process entrypoint so the follow-up mints
            # its own tr_id and manages _transition_context (root_id chains,
            # parent_id = this transition), instead of inheriting the
            # parent's tr_id via a direct change_state call. Failures of the
            # follow-up must not bubble into the current transition.
            return getattr(process, self._next_transition)(**kwargs)
        except Exception as error:
            transition_logger.error(error)
