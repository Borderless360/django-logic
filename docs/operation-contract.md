# Durable operations in 3.0

Existing `instance.process.action(...)` calls keep their return value and declaration behavior.
The additions below are optional. PostgreSQL state protection changes for every lock-taking transition.

## Submission identity and fair progress

```python
from django_logic.background import operation_options

with operation_options(
    request_key=f"orders:ship:{order.pk}:{shipping_revision}",
    fairness_key=f"client:{order.client_id}",
) as submission:
    order.process.ship()

job_id = submission.job_id
created = submission.created
```

The context applies to one background invocation. It does not add arguments to application hooks.
Repeating a retained request key with the same object, process class, action, actor, and inputs returns its original job.
This also works after that job finishes and the object's state changes.
Different inputs raise `RequestConflict`. A new key represents a new request, not another execution attempt.
The process class's module and qualified name form part of this identity. Renaming them changes the identity.
Application permission checks must still protect the endpoint that accepts submissions.

Request keys are global strings, up to 255 characters. Include an application namespace.
The `django-logic:` prefix is reserved for engine-created successors.
The database job ID remains stable across retries. Existing provider request IDs can remain in application records.
**Cleanup retains records with request keys indefinitely**, including required successors.
The application must choose when deleting those records is safe. Deletion ends duplicate-submission protection.
Records without request keys keep the existing cleanup policy.

For a declaration default, use `fairness_key="group"` or a callable accepting `instance, **kwargs`.
The callable must return a string. `operation_options` overrides this default.
Workers take turns between eligible fairness groups using shared database records.
A single group can use every free worker slot. Existing running work is not interrupted when another group arrives.
The empty key groups legacy submissions together. Queues still select which workers may run a job.
Fair selection does not enforce provider request limits or guarantee equal completion times.

The existing one-unfinished-job rule per object and process remains.
A different request key does not bypass it. `AlreadyInProgress` means the new request was **not accepted**.
Applications that combine refresh requests must persist their own dirty flag or requested version.

## Expected waiting

```python
from datetime import datetime, timedelta, timezone
from django_logic.background import Defer

raise Defer(
    until=datetime.now(timezone.utc) + timedelta(seconds=retry_after),
    reason="provider allowance",
)
```

The worker saves `eligible_at` and `deferral_reason`, then frees its execution slot.
The supplied deadline must include a timezone. With `USE_TZ=False`, the worker converts it to Django's default timezone for storage.
It does not increment `errors_count`, write a failed state, or run completion callbacks.
The next attempt starts from the beginning. With default atomic execution, the attempt's database writes roll back.
With `atomic=False`, previously committed receipts survive. Both modes still require idempotent external requests.
Direct execution and `retry_pending()` also respect `eligible_at`.
The operator's `--send` command refuses to override a future deferral.
`retry_status()` treats deferred jobs as waiting through `eligible_at`, with the normal retry window allowed for pickup afterward.

## Required follow-up

```python
BackgroundTransition(
    "generate", sources=["new"], target="ready",
    side_effects=[generate_artifact], required_next="dispatch",
)
BackgroundTransition(
    "dispatch", sources=["ready"], target="sent",
    side_effects=[dispatch_artifact],
)
```

`required_next` names one background transition on the same bound process.
The business record and job must use the same database; cross-database declarations are refused.
The successor receives the existing arguments and a new trace ID.
Its `parent_id` references the predecessor's trace ID. It inherits the predecessor's fairness key.
Its request key is `django-logic:required-next:<predecessor job ID>`.
The successor row, predecessor completion, and final state commit together.
For `atomic=False`, all these writes use the dedicated job connection.
No successor side effect runs inside this transaction.

The successor must be valid in the final state. If it cannot be saved, completion fails and the predecessor retries.
External effects therefore still need receipts or provider idempotency.
The required successor name is saved when the predecessor is submitted.
Deployments must keep that action and its serialized inputs compatible with outstanding records.

`callbacks` and `next_transition` retain their existing best-effort behavior.
Do not specify both `required_next` and `next_transition` on one declaration.
This release does not serialize arbitrary callbacks or provide cross-process required follow-ups.
Synchronous predecessors do not support `required_next`.
A background successor may perform bounded creation of application-owned child jobs.
Its own repeated execution must not repeat completed business effects.

## State protection and cancellation

On PostgreSQL, lock-taking transitions acquire the physical business-row lock.
They wait at most two seconds for another transaction that holds the row.
A holder that outlasts the wait causes `TransitionTemporarilyUnavailable` before application work starts, and the engine logs a warning that names the record.
A background job that meets this refusal records an error and retries after the normal pause. It does not wait quietly.
The database retains the lock through the actual outer commit or rollback.
Inside this transaction, synchronous side effects run in a savepoint. A database error in a side effect discards their writes and still records the failed state and runs the failure callbacks. Any other error keeps the writes made before it.
The cache gate remains for compatibility; it is not the authority for database-state exclusion.
SQLite remains useful for synchronous tests, but cannot verify concurrent state protection.

Default atomic state-writing background jobs retain the business-row lock during their transaction.
Targetless jobs without a failed state protect only finalization, so independent provider work can overlap.
`atomic=False` jobs retain a separate job-row lock and protect business state only when finalizing.
Allow for both database connections. Provider requests must be bounded.
A connection loss cannot undo an external request that was already sent.

```python
from django_logic.background import cancel_pending
result = cancel_pending(job_id)
```

Results are `cancelled`, `running`, `finished`, or `missing`.
The function does not wait for a running job. It does not change business state or invoke callbacks.
Applications remain responsible for their cancellation state and any earlier confirmed external effects.
An unfinished job between attempts can be cancelled. This is not a promise to interrupt an active provider request.

## Upgrade and rollback

The new job columns carry database defaults, so producers on an earlier version keep inserting rows while the migrations run.
A normal rolling deployment is enough: migrate, then restart web processes and workers.
Workers on an earlier version ignore deferred eligibility and fairness, so restart them promptly.
Enable the new declaration options only after every worker runs this version.
Rolling back to an earlier version is possible while no deferred or required-successor records are outstanding.
Rows written by this version keep their extra columns; an earlier version ignores them.

Existing job rows receive neutral defaults and preserve their previous identities and inputs.
The fairness migration creates shared cursor records for their existing group keys.
The operator output distinguishes normal waiting from retrying after an error.

Subclasses overriding `_enqueue_atomic` must accept and forward its new keyword arguments:
`submission`, `using`, `request_key`, and `fairness_key`.
The internal required-successor writer must retain the supplied database alias.
Calling the public transition method from inside completion is not a substitute.
