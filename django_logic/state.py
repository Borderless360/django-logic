from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import blake2b
from django.db import DEFAULT_DB_ALIAS, connections, transaction, OperationalError


#: How long a transition waits for another transaction that holds the
#: business row. Most holders are a web request or a job finalization
#: and commit within milliseconds. A holder that outlasts this wait is
#: refused, which keeps a long job from stalling the caller. Zero refuses
#: at once.
ROW_LOCK_WAIT_SECONDS = 2

_active_actions = ContextVar('django_logic_active_actions', default=())


class State:
    def __init__(self, instance, field_name: str, process_name=None):
        self.instance = instance
        self.field_name = field_name
        self.process_name = process_name

    @contextmanager
    def protected(self):
        """Prevent concurrent changes while allowing references from related rows."""
        using = self.instance._state.db or DEFAULT_DB_ALIAS
        model = type(self.instance)
        rows = model._base_manager.using(using)
        if connections[using].vendor != 'postgresql':
            with transaction.atomic(using=using):
                yield rows.get(pk=self.instance.pk)
            return
        from django_logic.exceptions import TransitionTemporarilyUnavailable, RefusalReason
        from django_logic.logger import transition_logger
        with transaction.atomic(using=using):
            try:
                with transaction.atomic(using=using):
                    declaring_model = self.instance._meta.get_field(self.field_name).model
                    row = declaring_model._base_manager.using(using)
                    if ROW_LOCK_WAIT_SECONDS <= 0:
                        fresh = row.select_for_update(nowait=True, no_key=True).get(pk=self.instance.pk)
                    else:
                        with connections[using].cursor() as cursor:
                            # The wait applies to this savepoint only. A refused
                            # lock rolls the savepoint back, which also restores
                            # the caller's own lock_timeout.
                            cursor.execute("SELECT current_setting('lock_timeout')")
                            previous = cursor.fetchone()[0]
                            cursor.execute("SELECT set_config('lock_timeout', %s, true)",
                                           [f'{int(ROW_LOCK_WAIT_SECONDS * 1000)}ms'])
                        fresh = row.select_for_update(no_key=True).get(pk=self.instance.pk)
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
            yield fresh if type(fresh) is model else rows.get(pk=self.instance.pk)

    @contextmanager
    def executing_action(self):
        from django_logic.exceptions import RefusalReason, TransitionNotAllowed

        using = self.instance._state.db or DEFAULT_DB_ALIAS
        key = (using, self.instance_key)
        active = _active_actions.get()
        if key in active:
            raise TransitionNotAllowed('A side effect already acts on this state field.', reason=RefusalReason.LOCKED)
        token = _active_actions.set((*active, key))
        try:
            yield
        finally:
            _active_actions.reset(token)

    def finish_action(self):
        # The target is saved. Required completion work can start the next action on this field.
        active = _active_actions.get()
        key = (self.instance._state.db or DEFAULT_DB_ALIAS, self.instance_key)
        if active and active[-1] == key:
            _active_actions.set(active[:-1])

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
        # Check the same database that stores the instance.
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
            # Callbacks and callers must see the state that the database accepted.
            setattr(self.instance, self.field_name, previous)
            raise
        self.instance.refresh_from_db(fields=[self.field_name], **database_kwargs)

    @property
    def instance_key(self):
        """One key per physical row and state column.

        The key names the table that holds the column, not the class the
        caller happened to load. A proxy model and its concrete model,
        and a multi-table-inheritance child and the parent that declares
        the column, all write the same column of the same row and share its lock.
        """
        declaring_model = self.instance._meta.get_field(self.field_name).model
        return f'{declaring_model._meta.db_table}-' \
               f'{self.field_name}-' \
               f'{self.instance.pk}'

    def get_state(self):
        return getattr(self.instance, self.field_name)

    def _get_hash(self):
        return blake2b(self.instance_key.encode(), digest_size=16).hexdigest()
