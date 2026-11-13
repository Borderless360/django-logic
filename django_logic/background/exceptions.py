from django_logic.exceptions import TransitionTemporarilyUnavailable


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
        Consumers whose side-effects derive data from mutable rows need
        a saved recheck: persist a dirty flag or version before submission
        and use a BackgroundEffect to check whether another job is needed.
        A success callback can be lost, so it cannot guarantee that recheck.
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
