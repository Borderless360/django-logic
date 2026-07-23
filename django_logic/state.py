from hashlib import blake2b
from uuid import uuid4

from django.core.cache import cache

from django_logic.conf import lock_timeout as _get_lock_timeout


class State(object):
    def __init__(self, instance, field_name: str, process_name=None):
        self.instance = instance
        self.field_name = field_name
        self.process_name = process_name

    def _remember_effective_timeout(self, timeout):
        """Resolve and remember the TTL to lock with (per-transition
        override or the global), so later refreshes keep the lifetime."""
        self._effective_lock_timeout = (
            timeout if timeout is not None else _get_lock_timeout()
        )
        return self._effective_lock_timeout

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

    def lock(self, timeout=None):
        """
        Atomically locks the state.
        Returns True if the lock was acquired, False if already locked.

        Stores a unique ownership token as the lock value, so a stale
        holder whose lock TTL-expired cannot release a successor's lock
        (see ``unlock``).

        ``timeout`` overrides the global ``LOCK_TIMEOUT`` for this lock
        (per-transition ``lock_timeout``); the effective value is
        remembered on the instance so later TTL refreshes (RedisState's
        ``set_state``) keep the same lifetime.
        """
        token = uuid4().hex
        if cache.add(self._get_hash(), token,
                     self._remember_effective_timeout(timeout)):
            self._lock_token = token
            return True
        return False

    def _stored_token(self, cached):
        """Extract the ownership token from a cached lock value."""
        return cached

    def unlock(self):
        """Release the lock — but only if this State object still owns it.

        Compare-and-delete on the ownership token issued by ``lock()``:
        if this holder's lock TTL-expired and another caller acquired the
        key since, the stored token no longer matches and the successor's
        lock is left intact. The get+compare+delete pair is not atomic on
        generic cache backends, but it shrinks the misdelete window from
        "always, after any takeover" to a takeover happening between the
        compare and the delete.

        A State object that never acquired the lock holds no token and
        falls back to an unconditional delete — the historical
        force-release behavior, kept for manual repair paths.
        """
        key = self._get_hash()
        token = getattr(self, '_lock_token', None)
        if token is None or self._stored_token(cache.get(key)) == token:
            cache.delete(key)

    def is_locked(self):
        """
        It checks whether the state was locked or not.
        It might return False due to the race conditions.
        However, `lock` method should guarantees it will be locked only once.
        """
        return cache.get(self._get_hash()) is not None


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
    _STATE_KEY = '__dl_state__'
    _TOKEN_KEY = '__dl_token__'

    @property
    def lock_timeout(self):
        # Prefer the TTL this instance locked with (per-transition
        # lock_timeout); fall back to the global for state objects that
        # never locked (e.g. phase 2's unlocked xx refreshes).
        return getattr(self, '_effective_lock_timeout', None) or _get_lock_timeout()

    def _store_value(self, state, token):
        """Wrap state + ownership token for single-key storage.

        The dict wrapper replaces the pre-0.9 raw state value; None
        states keep the sentinel so ``is_locked()`` works. ``_read_value``
        still understands raw legacy values, so keys written by an older
        version stay readable across an upgrade (they carry no token, so
        only a force-release can delete them until they are reacquired).
        """
        return {
            self._STATE_KEY: self._SENTINEL if state is None else state,
            self._TOKEN_KEY: token,
        }

    def _read_value(self, cached):
        """Unwrap the storage wrapper (or a raw legacy value) to the state."""
        if isinstance(cached, dict) and self._STATE_KEY in cached:
            cached = cached[self._STATE_KEY]
        if cached == self._SENTINEL:
            return None
        return cached

    def _stored_token(self, cached):
        if isinstance(cached, dict):
            return cached.get(self._TOKEN_KEY)
        # Raw legacy value written by a pre-token version: no ownership
        # information, never CAD-matched (force-release still works).
        return None

    def lock(self, timeout=None):
        self._remember_effective_timeout(timeout)
        token = uuid4().hex
        current = super().get_state()
        acquired = cache.set(
            self._get_hash(),
            self._store_value(current, token),
            self.lock_timeout,
            nx=True,
        ) or False
        if acquired:
            self._lock_token = token
        return acquired

    def is_locked(self):
        return cache.get(self._get_hash()) is not None

    def set_state(self, state):
        # xx=True: refresh the key's value/TTL only when it exists (i.e. the
        # state is locked). A state write outside a lock()/unlock() pair —
        # background phase 2's target/failed writes, Action.failed_state —
        # must not implicitly create a lock nobody will release. (Background
        # phase 1's in_progress write happens UNDER its critical-section
        # lock, so there the xx write refreshes the live key's value.)
        #
        # Preserve the ownership token already stored on the key: a state
        # write must not clobber the holder's token. Token-gated for
        # holders: if this object holds a token but the key now carries a
        # DIFFERENT one, our lock TTL-expired and a successor owns the
        # key — skip the cache refresh entirely, or we would re-plant our
        # own token over theirs and our later unlock would delete their
        # lock (the dual-entry hazard #139 closes). The DB write below
        # still happens; the phase-2 state guard / under-lock revalidation
        # arbitrate the outcome.
        #
        # The get→compare→set is still not multi-process atomic: a
        # takeover strictly between the read and the write can leave a
        # stale token behind. For tokenless writers that degrades to a
        # TTL-bounded leak; for a holder it remains a narrow wrong-unlock
        # window — fully closing it needs an atomic refresh (issue #151).
        current_token = self._stored_token(cache.get(self._get_hash()))
        own_token = getattr(self, '_lock_token', None)
        if (
            own_token is not None
            and current_token is not None
            and current_token != own_token
        ):
            super().set_state(state)
            return
        cache.set(
            self._get_hash(),
            self._store_value(state, current_token),
            self.lock_timeout,
            xx=True,
        )
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
