from enum import StrEnum


class RefusalReason(StrEnum):
    """Stable reason values for a refused transition."""

    PERMISSION = 'permission'
    CONDITION = 'condition'
    SOURCE_STATE = 'source_state'
    LOCKED = 'locked'
    BACKGROUND_IN_FLIGHT = 'background_in_flight'
    BACKGROUND_STRANDED = 'background_stranded'
    AMBIGUOUS = 'ambiguous'


class TransitionNotAllowed(Exception):
    """When the process resolver raises this, it sets ``current_state``
    on the exception. It stays ``None`` at other raise sites.
    ``reason`` describes the failed check. It stays ``None`` when a custom
    validity override supplies no reason, or the caller creates the exception.
    Process calls attach ``user_message`` for display. Diagnostic text and
    exception arguments remain unchanged.
    """

    current_state = None

    def __init__(self, *args, reason: RefusalReason | None = None, user_message=None):
        super().__init__(*args)
        self.reason = reason
        self.user_message = user_message


class TransitionTemporarilyUnavailable(TransitionNotAllowed):
    """Transient refusal: the transition is permitted but another
    transition owns the instance right now — retry shortly.

    Catch this AHEAD of ``TransitionNotAllowed`` to answer "busy" instead
    of "forbidden". Covers the background concurrency guard
    (``AlreadyInProgress``) and the
    unfinished-job check while the uncompleted ``TransitionMessage`` is still being
    retried — all of these resolve when that work completes. A row
    nothing is retrying is stranded, not busy, and raises the plain
    base: no worker will retry it automatically.
    A PostgreSQL row-lock timeout also raises this subclass, with reason ``locked``.
    A state lock refusal ("State is locked") raises plain ``TransitionNotAllowed``.
    The shared reason alone does not determine whether a short retry is appropriate.
    """
