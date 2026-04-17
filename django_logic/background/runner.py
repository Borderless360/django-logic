"""Phase 2 execution.

``run_background_transition(tm_id)`` owns a single attempt at executing
a durable background transition. It runs the same way in:

* the Celery task wrapper (:mod:`django_logic.background.tasks`), and
* sync mode, directly after phase 1 in the same process.

Structure:

1. One ``atomic`` block that:

   * locks the TransitionMessage row with ``select_for_update(nowait=True)``
     (another worker already holds it → raise ``OperationalError`` →
     caller exits silently),
   * restores the instance + transition,
   * runs each side-effect in order,
   * on success, writes ``target`` state (for ``BackgroundTransition``)
     and marks the TM completed,
   * on failure, records the error and either leaves the TM for retry
     or, at ``MAX_ERRORS``, writes ``failed_state`` and marks completed.

2. After the atomic block (best-effort):

   * success callbacks + ``next_transition`` (success path), or
   * failure callbacks (terminal-failure path).

Exceptions from side-effects propagate out of ``run_background_transition``
so the Celery task can decide to retry. In sync mode, they also
propagate to the original caller — tests can ``assertRaises`` directly.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.db import OperationalError, transaction

from django_logic.background import settings as bg_settings
from django_logic.background.models import TransitionMessage
from django_logic.background.serializers import restore_user
from django_logic.background.transitions import BackgroundAction, BackgroundTransition
from django_logic.logger import TransitionEventType, transition_logger
from django_logic.process import _transition_context


@dataclass
class _Outcome:
    """What phase 2's atomic block produced — drives best-effort phase 3."""

    terminal: bool  # Work is done (target, failed, or nothing to run)
    succeeded: bool
    exception: BaseException | None = None
    transition: BackgroundTransition | None = None
    state_obj: Any = None
    kwargs: dict | None = None


def run_background_transition(transition_message_id: int) -> None:
    """Run a single attempt at the transition identified by ``transition_message_id``.

    Designed to be call-compatible from both a Celery task and an
    inline sync dispatcher.
    """
    try:
        outcome = _run_atomic(transition_message_id)
    except _NothingToDo:
        return

    # Phase 3 (best-effort).
    if outcome.terminal and outcome.succeeded and outcome.transition is not None:
        _run_success_hooks(outcome)
    elif outcome.terminal and not outcome.succeeded and outcome.transition is not None:
        _run_failure_hooks(outcome)

    if outcome.exception is not None:
        raise outcome.exception


class _NothingToDo(Exception):
    """Internal signal: the TM is already completed, missing, or locked
    by another worker. Caller should exit silently."""


def _run_atomic(tm_id: int) -> _Outcome:
    with transaction.atomic():
        try:
            tm = (
                TransitionMessage.objects
                .select_for_update(nowait=True)
                .get(pk=tm_id, is_completed=False)
            )
        except TransitionMessage.DoesNotExist as exc:
            transition_logger.info(
                f'TransitionMessage#{tm_id} already completed or missing; '
                f'nothing to do'
            )
            raise _NothingToDo() from exc
        except OperationalError as exc:
            transition_logger.info(
                f'TransitionMessage#{tm_id} locked by another worker; '
                f'skipping this attempt'
            )
            raise _NothingToDo() from exc

        kwargs = dict(tm.kwargs or {})
        restore_user(kwargs)

        try:
            instance, process, transition = _restore(tm)
        except _Restore_Failed as exc:
            transition_logger.error(
                f'TransitionMessage#{tm.pk} cannot be restored: {exc}. '
                f'Marking completed to stop retries.'
            )
            tm.mark_as_completed()
            raise _NothingToDo() from exc

        state = process.state
        token = _transition_context.set(
            {
                'root_id': kwargs.get('root_id'),
                'tr_id': kwargs.get('tr_id'),
            }
        )
        try:
            transition_logger.info(
                f'{kwargs.get("tr_id")} Phase2 Start '
                f'{transition.action_name} {state.instance_key} '
                f'queue={tm.queue_name}'
            )
            try:
                for command in transition.side_effects.commands:
                    transition_logger.info(
                        f'{kwargs.get("tr_id")} '
                        f'{TransitionEventType.SIDE_EFFECT.value} '
                        f'{getattr(command, "__name__", repr(command))}'
                    )
                    command(instance, **kwargs)
            except Exception as error:
                return _handle_failure(tm, transition, state, kwargs, error)
            else:
                return _handle_success(tm, transition, state, kwargs)
        finally:
            _transition_context.reset(token)


def _handle_success(
    tm: TransitionMessage,
    transition: BackgroundTransition,
    state,
    kwargs: dict,
) -> _Outcome:
    if not isinstance(transition, BackgroundAction):
        state.set_state(transition.target)
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
            f'{transition.target}'
        )
    tm.mark_as_completed()
    transition_logger.info(
        f'{kwargs.get("tr_id")} {TransitionEventType.COMPLETE.value}'
    )
    return _Outcome(
        terminal=True,
        succeeded=True,
        transition=transition,
        state_obj=state,
        kwargs=kwargs,
    )


def _handle_failure(
    tm: TransitionMessage,
    transition: BackgroundTransition,
    state,
    kwargs: dict,
    error: BaseException,
) -> _Outcome:
    tm.record_error(error)
    transition_logger.error(
        f'{kwargs.get("tr_id")} {TransitionEventType.FAIL.value}: '
        f'{type(error).__name__}: {error}',
        exc_info=True,
    )

    max_errors = bg_settings.max_errors()
    if tm.errors_count < max_errors:
        # Leave uncompleted → periodic starter will retry.
        return _Outcome(
            terminal=False,
            succeeded=False,
            exception=error,
            transition=transition,
            state_obj=state,
            kwargs=kwargs,
        )

    # Terminal failure: write failed_state (if any) and mark completed.
    if transition.failed_state:
        state.set_state(transition.failed_state)
        transition_logger.info(
            f'{kwargs.get("tr_id")} {TransitionEventType.SET_STATE.value} '
            f'{transition.failed_state}'
        )
    # failure_side_effects run inside the atomic block, before unlock —
    # idempotent per the reliability contract.
    try:
        transition.failure_side_effects.execute(
            state, exception=error, **kwargs
        )
    except Exception:
        # FailureSideEffects already swallows internally, but belt-and-braces.
        pass
    tm.mark_as_completed()
    return _Outcome(
        terminal=True,
        succeeded=False,
        exception=error,
        transition=transition,
        state_obj=state,
        kwargs=kwargs,
    )


def _run_success_hooks(outcome: _Outcome) -> None:
    assert outcome.transition is not None
    try:
        outcome.transition.callbacks.execute(
            outcome.state_obj, **(outcome.kwargs or {})
        )
    except Exception as e:
        transition_logger.error(
            f'{(outcome.kwargs or {}).get("tr_id")} callbacks failed '
            f'(best-effort, swallowed): {e}',
            exc_info=True,
        )
    try:
        outcome.transition.next_transition.execute(
            outcome.state_obj, **(outcome.kwargs or {})
        )
    except Exception as e:
        transition_logger.error(
            f'{(outcome.kwargs or {}).get("tr_id")} next_transition failed '
            f'(best-effort, swallowed): {e}',
            exc_info=True,
        )


def _run_failure_hooks(outcome: _Outcome) -> None:
    assert outcome.transition is not None
    try:
        outcome.transition.failure_callbacks.execute(
            outcome.state_obj,
            exception=outcome.exception,
            **(outcome.kwargs or {}),
        )
    except Exception as e:
        transition_logger.error(
            f'{(outcome.kwargs or {}).get("tr_id")} failure_callbacks failed '
            f'(best-effort, swallowed): {e}',
            exc_info=True,
        )


class _Restore_Failed(Exception):
    """The TransitionMessage refers to a model/instance/transition that
    no longer exists. The TM is marked completed to stop the retry loop.
    """


def _restore(tm: TransitionMessage):
    """Resolve ``(instance, process, transition)`` from a TM row."""
    try:
        app = apps.get_app_config(tm.app_label)
        model = app.get_model(tm.model_name)
    except LookupError as exc:
        raise _Restore_Failed(
            f'model {tm.app_label}.{tm.model_name} not installed'
        ) from exc

    try:
        instance = model.objects.get(pk=tm.instance_id)
    except model.DoesNotExist as exc:
        raise _Restore_Failed(
            f'{tm.app_label}.{tm.model_name}#{tm.instance_id} not found'
        ) from exc

    try:
        process = getattr(instance, tm.process_name)
    except AttributeError:
        # Fall back to process_class stored in kwargs, if any.
        process_class_path = (tm.kwargs or {}).get('process_class')
        if not process_class_path:
            raise _Restore_Failed(
                f'instance has no process named {tm.process_name!r} and '
                f'no process_class stored on the message'
            )
        process = _load_process_from_path(instance, process_class_path, tm)

    transition = _find_transition(process, tm)
    if transition is None:
        raise _Restore_Failed(
            f'transition {tm.transition_name!r} not found on process '
            f'{type(process).__module__}.{type(process).__name__}'
        )
    return instance, process, transition


def _load_process_from_path(instance, dotted: str, tm: TransitionMessage):
    module_path, class_name = dotted.rsplit('.', 1)
    module = importlib.import_module(module_path)
    process_class = getattr(module, class_name)
    field_name = _infer_field_name(instance, tm)
    return process_class(field_name=field_name, instance=instance)


def _infer_field_name(instance, tm: TransitionMessage) -> str:
    # Best effort: if the model exposes a property with process_name,
    # pull the field name from its State. Otherwise default to 'state'.
    try:
        process = getattr(instance, tm.process_name)
        return process.field_name
    except Exception:
        return 'state'


def _find_transition(process, tm: TransitionMessage):
    # Look up by action_name, ignoring state membership — the instance's
    # state is presumably the transition's in_progress_state, which is
    # unique within a Process per _validate_unique_in_progress_states.
    for transition in process.transitions:
        if transition.action_name == tm.transition_name:
            return transition
    return None
