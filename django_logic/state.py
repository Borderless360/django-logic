from hashlib import blake2b
from django.conf import settings
from django.core.cache import cache
from django.utils.functional import cached_property

LOCK_TIMEOUT = getattr(settings, 'DJANGO_LOGIC', {}).get('LOCK_TIMEOUT', 7200)  # 2 hours by default


class State(object):
    def __init__(self, instance: any, field_name: str, process_name=None, queryset_name=None):
        self.instance = instance
        self.queryset_name = queryset_name or 'objects'
        self.field_name = field_name
        self.process_name = process_name

    def get_queryset(self):
        return getattr(self.instance._meta.model, self.queryset_name).all()

    def get_db_state(self):
        """
        Fetches state directly from db instead of model instance.
        """
        return self.get_queryset().values_list(self.field_name, flat=True).get(pk=self.instance.id)

    def set_state(self, state):
        """
        Sets intermediate state to instance's field until transition is over.
        """
        setattr(self.instance, self.field_name, state)
        # update with single instance save to apply overloaded save method
        self.instance.save(update_fields=[self.field_name])
        self.instance.refresh_from_db()

    @property
    def instance_key(self):
        return f'{self.instance._meta.app_label}-' \
               f'{self.instance._meta.model_name}-' \
               f'{self.field_name}-' \
               f'{self.instance.pk}'

    def get_log_data(self):
        return {
            'instance': self.instance,
            'queryset_name': self.queryset_name,
            'process_name': self.process_name,
            'field_name': self.field_name,
        }
    
    def get_state(self):
        return getattr(self.instance, self.field_name)

    def _get_hash(self):
        return blake2b(self.instance_key.encode(), digest_size=16).hexdigest()

    def lock(self):
        """
        It locks the state for 3 years.
        It returns True if it's been locked and False otherwise.
        """
        cache.set(self._get_hash(), True, LOCK_TIMEOUT)
        return True

    def unlock(self):
        """
        It unlocks the current state
        """
        cache.delete(self._get_hash())

    def is_locked(self):
        """
        It checks whether the state was locked or not.
        It might return False due to the race conditions.
        However, `lock` method should guarantees it will be locked only once.
        """
        return cache.get(self._get_hash()) or False


class RedisState(State):
    """
    RedisState uses a single Redis key for both locking and state storage.

    The key's existence means the state is locked; its value is the current
    state. This makes the state immediately visible to all processes
    regardless of DB transaction isolation.

    lock()      → atomically creates the key with the current state (nx=True)
    set_state() → overwrites the key value with the new state + persists to DB
                  (resets TTL so the lock stays alive while making progress)
    get_state() → reads from the key (fallback to instance attr when unlocked)
    unlock()    → deletes the key; DB is the source of truth again

    If the process crashes without calling unlock(), the key expires
    after lock_timeout seconds and the state becomes available again.
    """
    lock_timeout = LOCK_TIMEOUT

    def lock(self):
        current = super().get_state()
        return cache.set(self._get_hash(), current, self.lock_timeout, nx=True) or False

    def is_locked(self):
        return cache.get(self._get_hash()) is not None

    def set_state(self, state):
        cache.set(self._get_hash(), state, self.lock_timeout)
        super().set_state(state)

    def get_state(self):
        cached = cache.get(self._get_hash())
        if cached is not None:
            return cached
        return super().get_state()

    def get_db_state(self):
        cached = cache.get(self._get_hash())
        if cached is not None:
            return cached
        return super().get_db_state()
