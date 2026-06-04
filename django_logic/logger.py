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
    """Return the kwargs value to attach to a log record's ``extra``,
    honouring the ``DJANGO_LOGIC`` logging-privacy settings.

    Transition kwargs commonly carry a ``user`` object, the ``request``,
    and arbitrary business data (amounts, emails, tokens). By default they
    are logged as-is (backward compatible), but two opt-ins are available
    for PII/compliance-sensitive deployments:

    * ``DJANGO_LOGIC['LOG_KWARGS'] = False`` — never attach kwargs to log
      records (returns ``{}``).
    * ``DJANGO_LOGIC['LOG_KWARGS_REDACTOR'] = callable | 'dotted.path'`` —
      a callable given a shallow copy of the kwargs that returns a
      sanitised dict (e.g. drop ``user``/``request``, mask ``email``).

    A broken/raising redactor must never break a transition or silently
    leak the raw kwargs, so it degrades to a redaction marker.
    """
    conf = getattr(settings, 'DJANGO_LOGIC', {}) or {}
    if conf.get('LOG_KWARGS', True) is False:
        return {}
    redactor = conf.get('LOG_KWARGS_REDACTOR')
    if redactor is None:
        return kwargs
    try:
        if isinstance(redactor, str):
            from django.utils.module_loading import import_string
            redactor = import_string(redactor)
        return redactor(dict(kwargs))
    except Exception:
        return {'__redaction_error__': True}


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
