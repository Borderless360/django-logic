class DjangoLogicException(Exception):
    pass


# Coexistence base: DJANGO_LOGIC['LEGACY_EXCEPTION_BASE'], applied at
# ready() by conf.install_legacy_exception_base (#190).
class TransitionNotAllowed(DjangoLogicException):
    pass


class TransitionTemporarilyUnavailable(TransitionNotAllowed):
    """Transient refusal: the transition is permitted but another flight
    owns the instance right now — retry shortly.

    Catch this AHEAD of ``TransitionNotAllowed`` to answer "busy" instead
    of "forbidden". Covers the background concurrency guards
    (``AlreadyInProgress``, ``SourceStateChanged``) and the sync gate that
    rejects a transition while an uncompleted ``TransitionMessage`` exists
    — all three resolve when the in-flight work completes. Lock contention
    ("State is locked") deliberately stays plain ``TransitionNotAllowed``:
    a TTL-stuck lock is not "retry shortly", so widening this type to
    cover it would be a documented, deliberate decision — not drift.
    """
