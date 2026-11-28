from django_logic.exceptions import TransitionTemporarilyUnavailable


class _RestoreError(Exception):
    """A saved job's model, instance, transition or submitting user no longer exists."""


class _RecordedFailure(RuntimeError):
    """A saved failure reason and inert names of its original exception types."""

    def __init__(self, message, type_names):
        super().__init__(message)
        self.type_names = tuple(name for name in type_names if isinstance(name, str))


def failure_matches(exception, exception_type):
    """Match a failure's original type, including after the worker has restarted.

    ``exception_type`` is an exception class or tuple of classes. Saved names
    are compared as text; this function never imports or constructs a type.
    """
    types = exception_type if isinstance(exception_type, tuple) else (exception_type,)
    if any(not isinstance(cls, type) or not issubclass(cls, BaseException) for cls in types):
        raise TypeError('failure_matches expects an exception class or tuple of exception classes.')
    if isinstance(exception, _RecordedFailure):
        return any(f'{cls.__module__}.{cls.__qualname__}' in exception.type_names for cls in types)
    return isinstance(exception, types)


class PermanentFailure(Exception):
    """Raise from a background side-effect to say: another attempt gets the
    same answer, so do not retry.

    The worker then takes the terminal path on this first attempt — it
    writes ``failed_state`` (when declared), marks the row completed, and
    runs ``failure_callbacks`` — exactly as an exhausted retry does. Use it
    for a refusal (no record matched, a rule said no, the payload was
    rejected), never for a failure the next attempt might survive (a lost
    connection, a timeout, a deadlock).

    For an exception type you do not control, declare
    ``no_retry_on=(SomeError, ...)`` on the transition instead. The two
    compose.
    """


class AlreadyInProgress(TransitionTemporarilyUnavailable):
    """Raised before preparation when an unfinished job already exists
    for the target instance and process.

    .. warning::

        Swallowing this as "already queued, the running job will pick up
        my changes" is only safe while the existing attempt has NOT
        started. If the worker is already executing — has already read
        its inputs — that run commits a result computed from pre-update
        data and **the update's signal is lost**: nothing re-runs.
        Consumers whose work reads mutable rows need a saved business version.
        The ordinary job must compare that version before completing its work.
        It must keep required newer work pending instead of discarding its request.
        A success callback can be lost, so it cannot guarantee that check.
    """


class Defer(Exception):
    """Wait until a specified time without recording a failed attempt."""

    def __init__(self, *, until, reason=''):
        from datetime import datetime
        from django.utils import timezone
        if not isinstance(until, datetime) or timezone.is_naive(until):
            raise ValueError('Defer.until must be a timezone-aware datetime.')
        self.until = until
        self.reason = str(reason)
        super().__init__(self.reason)
