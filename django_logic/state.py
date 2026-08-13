from hashlib import blake2b
from uuid import uuid4

from django.core.cache import cache
from django.db import DEFAULT_DB_ALIAS

from django_logic.conf import lock_timeout as _get_lock_timeout


class State(object):
    def __init__(self, instance, field_name: str, process_name=None):
        self.instance = instance
        self.field_name = field_name
        self.process_name = process_name

    def get_persisted_state(self):
        """Read the state column straight from the database row.

        Subclasses must NOT override this with a cached read — it is the
        authoritative source used by the under-the-lock revalidation and
        the phase-2 state guard.

        Uses ``_base_manager`` so a filtered default manager (archived /
        soft-deleted rows hidden) cannot make a framework-level reload of
        an existing row raise ``DoesNotExist`` mid-transition.
        """
        model = type(self.instance)
        # Route to the instance's own connection. Every WRITE path is already
        # instance-aware (Model.save and refresh_from_db pass
        # hints={'instance': ...}; _release_lock reads instance._state.db), but
        # this read was unrouted, so on a non-default alias the under-lock
        # revalidation and the phase-2 state guard compared against whatever
        # row the DEFAULT database happened to hold.
        using = self.instance._state.db or DEFAULT_DB_ALIAS
        return (
            model._base_manager
            .using(using)
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
        previous = getattr(self.instance, self.field_name)
        setattr(self.instance, self.field_name, state)
        try:
            self.instance.save(update_fields=[self.field_name])
        except Exception:
            # Restore the attribute the database refused. setattr happens
            # before the write, so a rejected save used to leave the instance
            # holding a value the database never had — harmless while the
            # exception escaped everything, but the failure paths now swallow
            # a rejected state write, so this phantom instance reaches
            # failure_side_effects, failure_callbacks and the sync caller.
            setattr(self.instance, self.field_name, previous)
            raise
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

        Stores a unique ownership token as the lock value, so a stale
        holder whose lock TTL-expired cannot release a successor's lock
        (see ``unlock``).
        """
        token = uuid4().hex
        if cache.add(self._get_hash(), token, _get_lock_timeout()):
            self._lock_token = token
            return True
        return False

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
        if token is None or cache.get(key) == token:
            cache.delete(key)

    def is_locked(self):
        """
        It checks whether the state was locked or not.
        It might return False due to the race conditions.
        However, `lock` method should guarantees it will be locked only once.
        """
        return cache.get(self._get_hash()) is not None
