from hashlib import blake2b
from uuid import uuid4

from django.core.cache import cache

from django_logic.conf import lock_timeout as _get_lock_timeout


class State(object):
    def __init__(self, instance, field_name: str, process_name=None):
        self.instance = instance
        self.field_name = field_name
        self.process_name = process_name

    @staticmethod
    def _effective_timeout(timeout):
        """Resolve the TTL to lock with: per-transition override or the
        global. Pure — the caller remembers it only on a SUCCESSFUL
        acquisition, so a later failed ``lock()`` on the same object
        cannot clobber the TTL the held lock was taken with (RedisState's
        ``set_state`` refreshes reuse it; see ``lock_timeout``)."""
        return timeout if timeout is not None else _get_lock_timeout()

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
        effective = self._effective_timeout(timeout)
        token = uuid4().hex
        if cache.add(self._get_hash(), token, effective):
            self._lock_token = token
            self._effective_lock_timeout = effective
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

    # Atomic refresh (#151): rewrite the live key's value with a new TTL
    # only if it still holds exactly the bytes ``set_state`` based its
    # ownership decision on. A takeover (value changed) or TTL expiry
    # (value gone) since the read makes the GET mismatch, so the write is
    # skipped — a stale holder can never re-plant its token over a
    # successor's lock, and an unlocked writer can never recreate an
    # expired key (xx semantics). Runs server-side on django-redis.
    _REFRESH_CAS = (
        "if redis.call('GET', KEYS[1]) == ARGV[1] then "
        "redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3]) return 1 "
        "end return 0"
    )

    @staticmethod
    def _redis_conn():
        """The raw django-redis connection used for the atomic refresh, or
        ``None`` when the backend is not django-redis — the single-process
        test fake / LocMemCache, where the read-compare-write below cannot
        race and needs no server-side CAS."""
        if getattr(cache, 'client', None) is None:
            return None
        try:
            from django_redis import get_redis_connection
        except ImportError:
            return None
        try:
            return get_redis_connection('default')
        except Exception:
            return None

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
        effective = self._effective_timeout(timeout)
        token = uuid4().hex
        current = super().get_state()
        acquired = cache.set(
            self._get_hash(),
            self._store_value(current, token),
            effective,
            nx=True,
        ) or False
        if acquired:
            self._lock_token = token
            self._effective_lock_timeout = effective
        return acquired

    def is_locked(self):
        return cache.get(self._get_hash()) is not None

    def set_state(self, state):
        # Refresh the key's value/TTL only when it exists (xx semantics): a
        # state write outside a lock()/unlock() pair — background phase 2's
        # target/failed writes, Action.failed_state — must not implicitly
        # create a lock nobody will release. (Background phase 1's
        # in_progress write happens UNDER its critical-section lock, so
        # there it refreshes the live key.) The stored ownership token is
        # preserved: a state write must not clobber the holder's token, and
        # a stale holder whose key now carries a successor's token must not
        # re-plant its own (the dual-entry hazard #139 closes). The DB write
        # always lands; the phase-2 state guard / under-lock revalidation
        # arbitrate the outcome.
        #
        # On django-redis the read → decide → write is a single server-side
        # compare-and-set (#151): the write applies only if the key still
        # holds exactly the bytes the token decision was based on, so a
        # takeover landing strictly between read and write can no longer
        # misplace a token. Off django-redis (the single-process test fake /
        # LocMemCache) there is no concurrency, so the plain read-write is
        # already race-free.
        conn = self._redis_conn()
        if conn is None:
            self._set_state_fallback(state)
            return

        full_key = cache.client.make_key(self._get_hash())
        current_raw = conn.get(full_key)
        if current_raw is None:
            # No live key: an unlocked write must not recreate the lock.
            super().set_state(state)
            return

        current_token = self._stored_token(cache.client.decode(current_raw))
        own_token = getattr(self, '_lock_token', None)
        if (
            own_token is not None
            and current_token is not None
            and current_token != own_token
        ):
            # A successor already owns the key; leave it untouched.
            super().set_state(state)
            return

        new_raw = cache.client.encode(self._store_value(state, current_token))
        conn.eval(
            self._REFRESH_CAS, 1, full_key,
            current_raw, new_raw, int(self.lock_timeout * 1000),
        )
        super().set_state(state)

    def _set_state_fallback(self, state):
        """Non-atomic token-gated refresh for a single-process cache (the
        test fake / LocMemCache). Correct there because nothing can take
        the lock over between the read and the write."""
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
