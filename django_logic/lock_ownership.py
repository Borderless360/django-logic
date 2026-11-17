"""Keep state-lock ownership separate from the business transaction."""
from contextlib import contextmanager
from datetime import timedelta
import os
from uuid import uuid4

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, connections, models, transaction
from django.db.models.functions import Now


class JobProcessOwner(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    job_id = models.BigIntegerField(null=True, db_index=True)

    class Meta:
        app_label = 'django_logic_background'


class StateLock(models.Model):
    key = models.CharField(max_length=32, primary_key=True)
    token = models.UUIDField()
    expires_at = models.DateTimeField(db_index=True)
    process_owner = models.ForeignKey(JobProcessOwner, null=True, on_delete=models.PROTECT)

    class Meta:
        app_label = 'django_logic_background'


class JobProcessStopped(RuntimeError):
    """The worker has already removed this job process's permission to take locks."""


# Set only after fork. Threads in one job process share its ownership.
_process_owner = None


def set_process_owner(owner_id, job_id=None):
    global _process_owner
    _process_owner = (os.getpid(), owner_id, job_id)


def _current_process_owner():
    if _process_owner is None:
        return None
    pid, owner_id, job_id = _process_owner
    if pid != os.getpid():
        raise ImproperlyConfigured(
            'An application-created process cannot use its parent job process ownership. '
            'Save required work as a background job instead.'
        )
    return owner_id, job_id


@contextmanager
def _lock_connection():
    """Commit lock changes independently, and close the extra connection immediately."""
    source = connections[DEFAULT_DB_ALIAS]
    from django_logic.conf import sync_enabled
    if source.vendor == 'sqlite' and sync_enabled():
        # SQLite supports single-process business tests on their ordinary connection.
        yield DEFAULT_DB_ALIAS
        return
    if source.vendor != 'postgresql':
        raise ImproperlyConfigured(
            'State locks require the default PostgreSQL database. '
            'SQLite is supported only in the enabled test runtime.'
        )
    alias = f'_django_logic_locks_{uuid4().hex}'
    connection = source.copy(alias=alias)
    connection.settings_dict['OPTIONS'].pop('pool', None)
    connections[alias] = connection
    try:
        with transaction.atomic(using=alias):
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                for name, limit in [('lock_timeout', 2000), ('statement_timeout', 5000)]:
                    cursor.execute(
                        "SELECT set_config(name, LEAST(NULLIF(setting::int, 0), %s)::text, true) "
                        "FROM pg_settings WHERE name = %s", [limit, name],
                    )
            yield alias
    finally:
        connection.close()
        del connections[alias]


def acquire_lock(key, timeout):
    token = uuid4()
    ownership = _current_process_owner()
    owner_id, expected_job_id = ownership if ownership is not None else (None, None)
    with _lock_connection() as alias:
        connection = connections[alias]
        if connection.vendor == 'postgresql':
            quote = connection.ops.quote_name
            with connection.cursor() as cursor:
                if owner_id is not None:
                    # A worker must wait for this short acquisition to commit before
                    # removing the owner. A removed owner can never be recreated here.
                    cursor.execute(
                        f'SELECT job_id FROM {quote(JobProcessOwner._meta.db_table)} '
                        'WHERE id = %s FOR KEY SHARE', [owner_id],
                    )
                    owner = cursor.fetchone()
                    if owner is None or owner[0] != expected_job_id:
                        raise JobProcessStopped('This job process has already stopped; no lock was taken.')
                cursor.execute(
                    f'INSERT INTO {quote(StateLock._meta.db_table)} '
                    '(key, token, expires_at, process_owner_id) '
                    'VALUES (%s, %s, statement_timestamp() + %s, %s) '
                    'ON CONFLICT (key) DO UPDATE SET token = EXCLUDED.token, '
                    'expires_at = EXCLUDED.expires_at, process_owner_id = EXCLUDED.process_owner_id '
                    f'WHERE {quote(StateLock._meta.db_table)}.expires_at <= statement_timestamp() '
                    'RETURNING expires_at', [key, token, timedelta(seconds=timeout), owner_id],
                )
                held = cursor.fetchone()
                acquired = held is not None
        else:
            with transaction.atomic(using=alias):
                rows = StateLock.objects.using(alias)
                rows.filter(key=key, expires_at__lte=Now()).delete()
                rows.bulk_create([
                    StateLock(key=key, token=token, expires_at=Now() + timedelta(seconds=timeout)),
                ], ignore_conflicts=True)
                acquired = rows.filter(key=key, token=token).exists()
    # A foreign-key failure at commit must never let business work start.
    return token if acquired else None


def release_lock(key, token):
    with _lock_connection() as alias:
        connection = connections[alias]
        token = StateLock._meta.get_field('token').get_db_prep_value(token, connection)
        with connection.cursor() as cursor:
            # Keep token comparison in the DELETE itself. Django's deletion
            # collector can otherwise select IDs first when signals are installed.
            cursor.execute(
                f'DELETE FROM {connection.ops.quote_name(StateLock._meta.db_table)} '
                'WHERE key = %s AND token = %s', [key, token],
            )


def is_locked(key):
    with _lock_connection() as alias:
        return StateLock.objects.using(alias).filter(key=key, expires_at__gt=Now()).exists()


def create_process_owner(job_id=None, *, owner_id=None):
    """Record a process identity; the caller commits before starting the process."""
    return JobProcessOwner.objects.using(DEFAULT_DB_ALIAS).create(
        id=owner_id or uuid4(), job_id=job_id,
    ).pk


def jobs_with_state_locks():
    return StateLock.objects.using(DEFAULT_DB_ALIAS).filter(
        process_owner__job_id__isnull=False, expires_at__gt=Now(),
    ).values('process_owner__job_id')


def another_process_holds_job_locks(job_id):
    ownership = _current_process_owner()
    with _lock_connection() as alias:
        rows = StateLock.objects.using(alias).filter(
            process_owner__job_id=job_id, expires_at__gt=Now(),
        )
        if ownership is not None:
            rows = rows.exclude(process_owner_id=ownership[0])
        return rows.exists()


def cleanup_expired_locks():
    """Remove expired SQL records in one bounded batch; retain unknown process identities."""
    with _lock_connection() as alias:
        connection = connections[alias]
        quote = connection.ops.quote_name
        table = quote(StateLock._meta.db_table)
        with connection.cursor() as cursor:
            if connection.vendor == 'postgresql':
                cursor.execute(
                    f'WITH expired AS (SELECT key FROM {table} WHERE expires_at <= statement_timestamp() '
                    'ORDER BY expires_at LIMIT 1000 FOR UPDATE SKIP LOCKED) '
                    f'DELETE FROM {table} WHERE key IN (SELECT key FROM expired) '
                    'AND expires_at <= statement_timestamp()',
                )
            else:
                expired = StateLock.objects.using(alias).filter(expires_at__lte=Now()).order_by('expires_at')
                keys = list(expired.values_list('key', flat=True)[:1000])
                StateLock.objects.using(alias).filter(key__in=keys, expires_at__lte=Now()).delete()


def retire_process_owner(owner_id, *, using=DEFAULT_DB_ALIAS):
    """Remove only a confirmed stopped process's locks, inside the worker's bounded transaction."""
    connection = connections[using]
    if not connection.in_atomic_block:
        raise ImproperlyConfigured('Removing job process ownership requires a transaction.')
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT id FROM {quote(JobProcessOwner._meta.db_table)} WHERE id = %s FOR UPDATE',
            [owner_id],
        )
        if cursor.fetchone() is None:
            return
        # Keep the owner condition in the actual DELETE. A newer token can have
        # replaced an expired lock while this transaction waited for its row.
        cursor.execute(
            f'DELETE FROM {quote(StateLock._meta.db_table)} WHERE process_owner_id = %s',
            [owner_id],
        )
        cursor.execute(
            f'DELETE FROM {quote(JobProcessOwner._meta.db_table)} WHERE id = %s', [owner_id],
        )


def retire_unstarted_owner(owner_id, *, using=DEFAULT_DB_ALIAS):
    """Resolve an uncertain registration commit before removing its exact process record."""
    connection = connections[using]
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        # The unique-ID check waits for an earlier registration transaction,
        # including one whose COMMIT reply was lost. A plain DELETE could miss
        # that transaction's not-yet-visible row.
        cursor.execute(
            f'INSERT INTO {quote(JobProcessOwner._meta.db_table)} '
            '(id, created_at, job_id) '
            'VALUES (%s, statement_timestamp(), NULL) '
            'ON CONFLICT (id) DO NOTHING', [owner_id],
        )
    retire_process_owner(owner_id, using=using)
