from django_logic.exceptions import TransitionTemporarilyUnavailable


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
