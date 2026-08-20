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
    """Raised when enqueue finds an uncompleted TransitionMessage already
    exists for the target instance + process (the partial unique
    constraint fires).

    .. warning::

        Swallowing this as "already queued, the running job will pick up
        my changes" is only safe while the existing attempt has NOT
        started. If the worker is already executing — has already read
        its inputs — that run commits a result computed from pre-update
        data and **the update's signal is lost**: nothing re-runs.
        Consumers whose side-effects derive data from mutable rows need
        a recheck: persist a dirty flag / version before dispatching,
        clear it inside the side-effect, and dispatch again from a
        success callback when it is still set.
    """


class SourceStateChanged(TransitionTemporarilyUnavailable):
    """Raised when the persisted state left the transition's sources
    while the insert waited on the unique constraint.

    A named subclass because this is an expected concurrency outcome —
    the guard doing its job, the same class of event as
    ``AlreadyInProgress``; both share ``TransitionTemporarilyUnavailable``,
    which is why the hook runner logs them at WARNING rather than ERROR.
    Consumers that treat it distinctly can catch it by type; everything
    catching ``TransitionNotAllowed`` keeps working.
    """
