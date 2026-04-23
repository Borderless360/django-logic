"""Structured logging for django-logic.

Two standard Python loggers are exposed:

- ``django-logic`` — general library activity
- ``django-logic.transition`` — per-transition lifecycle events; every
  record includes ``tr_id`` in the message body so lines for one logical
  transition can be grepped together.

Configure both via ``LOGGING`` in Django settings.
"""
import logging
from enum import Enum


logger: logging.Logger = logging.getLogger('django-logic')
transition_logger: logging.Logger = logging.getLogger('django-logic.transition')


class TransitionEventType(Enum):
    START = 'Start'
    COMPLETE = 'Complete'
    FAIL = 'Fail'
    SIDE_EFFECT = 'SideEffect'
    CALLBACK = 'Callback'
    FAILURE_SIDE_EFFECT = 'FailureSideEffect'
    SET_STATE = 'Set State'
    LOCK = 'Lock'
    UNLOCK = 'Unlock'
    NEXT_TRANSITION = 'Next Transition'
