"""Structured logging for django-logic.

Two standard Python loggers are exposed:

- ``django-logic`` — general library activity
- ``django-logic.transition`` — transition and worker events. Transition calls include ``tr_id`` when available.
  Other records identify a job or model instance instead; not every record has a trace ID.

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
    SET_STATE = 'Set State'
    LOCK = 'Lock'
    UNLOCK = 'Unlock'
