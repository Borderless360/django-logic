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

from django.conf import settings


logger: logging.Logger = logging.getLogger('django-logic')
transition_logger: logging.Logger = logging.getLogger('django-logic.transition')


def redact_log_kwargs(kwargs: dict) -> dict:
    """The kwargs value to attach to a log record's ``extra``.

    A shallow copy, not the live dict: log records are formatted lazily and
    the caller keeps mutating kwargs after the log call (``restore_user``
    pops ``user_id``, nested transitions rewrite ``tr_id``/``parent_id``), so
    sharing the reference would let later mutations leak into an
    already-emitted record.
    """
    return dict(kwargs)


class TransitionEventType(Enum):
    START = 'Start'
    COMPLETE = 'Complete'
    FAIL = 'Fail'
    SIDE_EFFECT = 'SideEffect'
    CALLBACK = 'Callback'
    SET_STATE = 'Set State'
    LOCK = 'Lock'
    UNLOCK = 'Unlock'
    NEXT_TRANSITION = 'Next Transition'
