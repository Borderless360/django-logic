from hashlib import blake2b
from django.conf import settings
from django.core.cache import cache


def _get_lock_timeout():
    """Read LOCK_TIMEOUT from settings on every call (not cached at import time)."""
    return getattr(settings, 'DJANGO_LOGIC', {}).get('LOCK_TIMEOUT', 7200)


class State(object):
    def __init__(self, instance: any, field_name: str, process_name=None):
        self.instance = instance
        self.field_name = field_name
        self.process_name = process_name

    def get_db_state(self):
        """
        Fetches state directly from db instead of model instance.
        """
        return self.get_persisted_state()

    def get_persisted_state(self):
        """Read the state column straight from the database row.

        Unlike ``get_db_state``, subclasses must NOT override this with a
        cached read — it is the authoritative source used by the
        under-the-lock revalidation and the phase-2 state guard.

        Uses ``_base_manager`` so a filtered default manager (archived /
        soft-deleted rows hidden) cannot make a framework-level reload of
        an existing row raise ``DoesNotExist`` mid-transition.
        """
        model = type(self.instance)
        return (
            model._base_manager
            .values_list(self.field_name, flat=True)
            .get(pk=self.instance.pk)
        )

    def set_state(self, state):
        """Persist the state field without touching other in-memory fields.

        ``update_fields=[self.field_name]`` respects custom ``save()``
        overrides. ``refresh_from_db(fields=[self.field_name])`` only
        re-reads the state column — any side-effect mutations on other
        attributes survive.
        """
        setattr(self.instance, self.field_name, state)
        self.instance.save(update_fields=[self.field_name])
        self.instance.refresh_from_db(fields=[self.field_name])

    @property
    def instance_key(self):
        return f'{self.instance._meta.app_label}-' \
               f'{self.instance._meta.model_name}-' \
               f'{self.field_name}-' \
               f'{self.instance.pk}'

    def get_state(self):
        return getattr(self.instance, self.field_name)

    def _get_hash(self):
        return blake2b(self.instance_key.encode(), digest_size=16).hexdigest()

    def lock(self):
        """
        Atomically locks the state.
        Returns True if the lock was acquired, False if already locked.
        """
        return cache.add(self._get_hash(), True, _get_lock_timeout())

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

    Requires ``django-redis`` (installed as a core dependency) as the cache
    backend. Django's built-in ``RedisCache`` does not support the
    ``nx=True`` / ``xx=True`` parameters used by ``lock()`` / ``set_state()``.

    The key's existence means the state is locked; its value is the current
    state. This makes the state immediately visible to all processes
    regardless of DB transaction isolation.

    lock()      -> atomically creates the key with the current state (nx=True)
    set_state() -> updates the key value with the new state *only if the key
                   already exists* (xx=True, resetting the TTL so the lock
                   stays alive while making progress) + persists to DB.
                   Writing state never CREATES a lock — only lock() does.
                   Under the lock (sync transitions; background phase 1's
                   in_progress write) the xx write refreshes the live key;
                   outside any lock (background phase 2's target/failed
                   writes, Action.failed_state) it is a cache no-op — which
                   is what lets background transitions use RedisState
                   without stranding the instance locked until TTL expiry.
    get_state() -> reads from the key (fallback to instance attr when unlocked)
    unlock()    -> deletes the key; DB is the source of truth again

    If the process crashes without calling unlock(), the key expires
    after lock_timeout seconds and the state becomes available again.
    """
    _SENTINEL = '__django_logic_locked__'

    @property
    def lock_timeout(self):
        return _get_lock_timeout()

    def _store_value(self, state):
        """Wrap None state values with a sentinel so is_locked() works."""
        return self._SENTINEL if state is None else state

    def _read_value(self, cached):
        """Unwrap sentinel back to None."""
        if cached == self._SENTINEL:
            return None
        return cached

    def lock(self):
        current = super().get_state()
        return cache.set(self._get_hash(), self._store_value(current), self.lock_timeout, nx=True) or False

    def is_locked(self):
        return cache.get(self._get_hash()) is not None

    def set_state(self, state):
        # xx=True: refresh the key's value/TTL only when it exists (i.e. the
        # state is locked). A state write outside a lock()/unlock() pair —
        # background phase 2's target/failed writes, Action.failed_state —
        # must not implicitly create a lock nobody will release. (Background
        # phase 1's in_progress write happens UNDER its critical-section
        # lock, so there the xx write refreshes the live key's value.)
        cache.set(self._get_hash(), self._store_value(state), self.lock_timeout, xx=True)
        super().set_state(state)

    def get_state(self):
        cached = cache.get(self._get_hash())
        if cached is not None:
            return self._read_value(cached)
        return super().get_state()

    def get_db_state(self):
        cached = cache.get(self._get_hash())
        if cached is not None:
            return self._read_value(cached)
        return super().get_db_state()
