from contextvars import ContextVar
from enum import StrEnum


class RefusalReason(StrEnum):
    """Stable reason values for a refused transition."""

    PERMISSION = 'permission'
    CONDITION = 'condition'
    SOURCE_STATE = 'source_state'
    UNKNOWN_ACTION = 'unknown_action'
    LOCKED = 'locked'
    BACKGROUND_IN_FLIGHT = 'background_in_flight'
    BACKGROUND_STRANDED = 'background_stranded'
    AMBIGUOUS = 'ambiguous'


# A declaration is shared across callers. Keep diagnostic state on this call.
_validity_refusal = ContextVar('_validity_refusal', default=None)


def _record_validity_refusal(owner, reason):
    capture = _validity_refusal.get()
    if capture is not None and capture[0] is owner:
        capture[1] = reason


def _check_validity(owner, *args):
    """Call the existing override once and capture its stock guard result."""
    capture = [owner, None]
    token = _validity_refusal.set(capture)
    try:
        valid = owner.is_valid(*args)
        return valid, capture[1] if not valid else None
    finally:
        _validity_refusal.reset(token)


class TransitionNotAllowed(Exception):
    """When the process resolver raises this, it sets ``current_state``
    and ``available_actions`` on the instance so an API layer does not
    have to reconstruct them. Both stay ``None`` on other raise sites.
    ``reason`` describes the failed check. It stays ``None`` for an opaque
    custom validity override or a caller-created exception.
    """

    current_state = None
    available_actions = None

    def __init__(self, *args, reason: RefusalReason | None = None):
        super().__init__(*args)
        self.reason = reason


class TransitionTemporarilyUnavailable(TransitionNotAllowed):
    """Transient refusal: the transition is permitted but another
    transition owns the instance right now — retry shortly.

    Catch this AHEAD of ``TransitionNotAllowed`` to answer "busy" instead
    of "forbidden". Covers the background concurrency guards
    (``AlreadyInProgress``, the source-state recheck at enqueue) and the
    sync gate while the uncompleted ``TransitionMessage`` is still being
    retried — all of these resolve when that work completes. A row
    nothing is retrying is stranded, not busy, and raises the plain
    base: it has no TTL, so "retry shortly" would be wrong forever. Lock
    contention ("State is locked") stays plain ``TransitionNotAllowed``
    for the same reason: a TTL-stuck lock is not "retry shortly".
    """
