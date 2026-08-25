class TransitionNotAllowed(Exception):
    """When the process resolver raises this, it sets ``current_state``
    and ``available_actions`` on the instance so an API layer does not
    have to reconstruct them. Both stay ``None`` on other raise sites.
    """

    current_state = None
    available_actions = None


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
