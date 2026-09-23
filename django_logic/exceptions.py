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
    if capture is not None and capture['owner'] is owner:
        capture['reason'] = reason


def _record_failed_check(bundle, command):
    capture = _validity_refusal.get()
    if capture is not None and capture['bundle'] is bundle:
        capture['failed_check'] = command


def _execute_validity_bundle(owner, bundle, *args):
    """Capture only checks from this owner's active bundle."""
    capture = _validity_refusal.get()
    if capture is None or capture['owner'] is not owner:
        return bundle.execute(*args)
    previous = capture['bundle'], capture['failed_check']
    capture['bundle'], capture['failed_check'] = bundle, None
    try:
        valid = bundle.execute(*args)
        capture['message'] = (
            getattr(capture['failed_check'], '_django_logic_refusal_message', None)
            if not valid else None
        )
        return valid
    finally:
        capture['bundle'], capture['failed_check'] = previous


def _check_validity(owner, *args):
    """Call the existing override once and capture its stock guard result."""
    capture = {'owner': owner, 'reason': None, 'message': None,
               'bundle': None, 'failed_check': None}
    token = _validity_refusal.set(capture)
    try:
        valid = owner.is_valid(*args)
        return (
            valid,
            capture['reason'] if not valid else None,
            capture['message'] if not valid else None,
        )
    finally:
        _validity_refusal.reset(token)


class TransitionNotAllowed(Exception):
    """When the process resolver raises this, it sets ``current_state``
    and ``available_actions`` on the instance so an API layer does not
    have to reconstruct them. Both stay ``None`` on other raise sites.
    ``reason`` describes the failed check. It stays ``None`` when a custom
    validity override supplies no reason, or the caller creates the exception.
    Process calls attach ``user_message`` for display. Diagnostic text and
    exception arguments remain unchanged.
    """

    current_state = None
    available_actions = None
    user_message = None

    def __init__(self, *args, reason: RefusalReason | None = None, user_message=None):
        super().__init__(*args)
        self.reason = reason
        self.user_message = user_message


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
