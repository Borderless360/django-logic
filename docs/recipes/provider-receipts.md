# Preserve provider receipts when later work fails

A provider can accept a request before a later report fails. With the default
atomic transition, that failure also rolls back the locally saved confirmation.
A retry cannot distinguish an accepted request from one never sent.

Use `BackgroundTransition(atomic=False)` when those confirmations must survive:

```python
class ManifestProcess(Process):
    transitions = [
        BackgroundTransition(
            action_name='submit',
            sources=['pending'],
            target='submitted',
            in_progress_state='submitting',
            failed_state='failed',
            conditions=[has_parcels],
            side_effects=[submit_to_provider, prepare_report],
            callbacks=[notify_parent],
            atomic=False,
            queue='manifests',
            timeout=600,
        ),
    ]
```

Side effects run directly in the normal job process. Each committed model write
survives a later exception, timeout, or process crash. Use small `transaction.atomic()`
blocks inside a side effect when several local writes must succeed together.
The engine still commits the final state and job completion together.
A rejected final state does not discard earlier provider confirmations.

The application must protect every request that cannot safely repeat:

1. Commit a pending receipt before making the request.
2. Save the confirmed response immediately after receiving it.
3. On retry, reuse confirmed responses and continue only unfinished work.
4. For a pending receipt, inspect the provider's record or raise `PermanentFailure`
   with a message requesting review. Do not blindly submit again.

A provider's idempotency key can also protect retries. The engine does not invent
provider keys, interpret responses, or promise that external requests happen once.

Only models and jobs reading and writing the default PostgreSQL database are
supported. Other database routes are refused before enqueue. Inline sync execution
requires autocommit; use `TransactionTestCase` for these tests. Pull enqueue may
run inside a caller transaction because execution starts after commit.

Custom model saves and signal handlers must honor Django's `using` argument for
writes that belong to the final state transaction. A custom `State.set_state`
override opting into this mode must accept and forward `using`.

Each running job uses an extra database connection to hold its job-row lock.
That temporary connection does not create a Django connection pool.
Transaction-pooling connections are supported; no session advisory lock is used.
Configure the database idle-transaction timeout above the job's declared timeout.
If that connection disappears, another worker can claim the job while an external
request is still running. The pending receipt or provider idempotency key protects
that request. The engine refuses to start another side effect after detecting the
lost connection.

Callbacks remain best effort. Required reports and emails belong in `side_effects`,
with their own saved delivery record and protection against repeated sends.
The default `atomic=True` transition still rolls back failed attempts' model writes.
