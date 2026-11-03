"""Recent worker observations, separate from progress on a queue."""
from hashlib import sha256

from django.core.cache import cache

from django_logic import conf
from django_logic.logger import logger


def _queue_key(queue_name):
    digest = sha256(queue_name.encode()).hexdigest()
    return f'django_logic:queue_served:{digest}'


def record_worker_presence(queues):
    """Record queues from the worker's bounded maintenance process."""
    values = {_queue_key(queue): True for queue in queues}
    if not values:
        return
    try:
        failed = cache.set_many(values, timeout=conf.retry_window_minutes() * 60)
        if failed:
            logger.warning('pull: some worker presence records could not be saved.')
    except Exception:
        logger.warning('pull: worker presence could not be saved; queue progress checks remain active.')


def queue_worker_presence(queue_name):
    """Return recent presence, absence, or ``None`` when the cache cannot answer.

    The record expires after the retry window. Workers do not delete it on
    exit because another worker may serve the same queue.
    """
    try:
        return cache.get(_queue_key(queue_name)) is True
    except Exception:
        return None
