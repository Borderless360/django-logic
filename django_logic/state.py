from contextlib import contextmanager
from hashlib import blake2b
from uuid import uuid4

from django.core.cache import cache
from django.db import DEFAULT_DB_ALIAS, connections, transaction, OperationalError

from django_logic.conf import lock_timeout as _get_lock_timeout

#: How long a transition waits for another transaction that holds the
#: business row. Most holders are a web request or a job finalization
#: and commit within milliseconds. A holder that outlasts this wait is
#: refused, which keeps a long job from stalling the caller. Zero refuses
#: at once.
ROW_LOCK_WAIT_SECONDS = 2


class State:
    def __init__(self, instance, field_name: str, process_name=None):
        self.instance = instance
        self.field_name = field_name
        self.process_name = process_name

    @contextmanager
    def protected(self):
        """Keep PostgreSQL state protection until the caller's transaction commits."""
        using = self.instance._state.db or DEFAULT_DB_ALIAS
        if connections[using].vendor != 'postgresql':
            yield
            return
        from django_logic.exceptions import TransitionTemporarilyUnavailable, RefusalReason
        from django_logic.logger import transition_logger
        with transaction.atomic(using=using):
            try:
                with transaction.atomic(using=using):
                    row = self.instance._meta.get_field(self.field_name).model._base_manager.using(using)
                    if ROW_LOCK_WAIT_SECONDS <= 0:
                        row.select_for_update(nowait=True).get(pk=self.instance.pk)
                    else:
                        with connections[using].cursor() as cursor:
                            # The wait applies to this savepoint only. A refused
                            # lock rolls the savepoint back, which also restores
                            # the caller's own lock_timeout.
                            cursor.execute("SELECT current_setting('lock_timeout')")
                            previous = cursor.fetchone()[0]
                            cursor.execute("SELECT set_config('lock_timeout', %s, true)",
                                           [f'{int(ROW_LOCK_WAIT_SECONDS * 1000)}ms'])
                        row.select_for_update().get(pk=self.instance.pk)
                        with connections[using].cursor() as cursor:
                            cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous])
            except OperationalError as error:
                cause = error.__cause__
                if (getattr(cause, 'sqlstate', None) or getattr(cause, 'pgcode', None)) == '55P03':
                    transition_logger.warning(
                        f'{self.instance_key}: another transaction held the record for more than '
                        f'{ROW_LOCK_WAIT_SECONDS} seconds; the transition is refused.'
                    )
                    raise TransitionTemporarilyUnavailable(
                        'Another transaction is changing this record. Try again shortly.',
                        reason=RefusalReason.LOCKED,
                        user_message='Another transaction is changing this record. Try again shortly.',
                    ) from error
                raise
            yield

    def get_persisted_state(self):
        """Read the state column straight from the database row.

        Subclasses must NOT override this with a cached read — it is the
        authoritative source used by the under-the-lock revalidation and
        the worker's state guard.

        Uses ``_base_manager`` so a filtered default manager (archived /
        soft-deleted rows hidden) cannot make a framework-level reload of
        an existing row raise ``DoesNotExist`` mid-transition.
        """
        model = type(self.instance)
        # Route to the instance's own connection. Every WRITE path is already
        # instance-aware (Model.save and refresh_from_db pass
        # hints={'instance': ...}), but this read was unrouted, so on a
        # non-default alias the under-lock revalidation and the worker's
        # state guard compared against whatever row the DEFAULT database
        # happened to hold.
        using = self.instance._state.db or DEFAULT_DB_ALIAS
        return (
            model._base_manager
            .using(using)
            .values_list(self.field_name, flat=True)
            .get(pk=self.instance.pk)
        )

    def set_state(self, state, *, using=None):
        """Persist the state field without touching other in-memory fields.

        ``update_fields=[self.field_name]`` respects custom ``save()``
        overrides. ``refresh_from_db(fields=[self.field_name])`` only
        re-reads the state column — any side-effect mutations on other
        attributes survive.
        """
        database_kwargs = {'using': using} if using is not None else {}
        previous = getattr(self.instance, self.field_name)
        setattr(self.instance, self.field_name, state)
        try:
            self.instance.save(update_fields=[self.field_name], **database_kwargs)
        except Exception:
            # Restore the attribute the database refused. setattr happens
            # before the write, so a rejected save used to leave the instance
            # holding a value the database never had — harmless while the
            # exception escaped everything, but the failure paths now swallow
            # a rejected state write, so this phantom instance reaches
            # failure_callbacks and the sync caller.
            setattr(self.instance, self.field_name, previous)
            raise
        self.instance.refresh_from_db(fields=[self.field_name], **database_kwargs)

    @property
    def instance_key(self):
        """One key per physical row and state column.

        The key names the table that holds the column, not the class the
        caller happened to load. A proxy model and its concrete model,
        and a multi-table-inheritance child and the parent that declares
        the column, all write the same column of the same row. Keyed by
        class name they took different locks, so two transitions ran on
        one row at the same time.
        """
        declaring_model = self.instance._meta.get_field(self.field_name).model
        return f'{declaring_model._meta.db_table}-' \
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
        """Whether the lock key exists right now. Racy — a lock can appear
        or expire between this read and the caller's next step; ``lock()``
        is the only atomic gate."""
        return cache.get(self._get_hash()) is not None
