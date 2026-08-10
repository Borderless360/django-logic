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
    (``AlreadyInProgress``, ``SourceStateChanged``) and the sync gate while
    the uncompleted ``TransitionMessage`` is LIVE — all of these resolve
    when the in-flight work completes. A row untouched past the retry
    horizon is stranded, not busy, and raises the plain base (#195): it
    has no TTL, so "retry shortly" would be wrong forever. Lock contention
    ("State is locked") stays plain ``TransitionNotAllowed`` for the same
    reason: a TTL-stuck lock is not "retry shortly".
    """
