# Durable operations in 3.0

Existing `instance.process.action(...)` calls keep their return value and declaration behavior.
The additions below are optional. PostgreSQL state protection changes for every lock-taking transition.

## Checking eligibility while another action runs

`process.get_available_actions(..., include_locked=True)` and
`process.get_available_transitions(..., include_locked=True)` check the object's
current state, conditions and permissions even when another action holds its
state lock. This lets an application distinguish a temporary wait from a business
rule that forbids an action. The default continues to hide actions that need a
held lock.

These queries do not acquire or release locks, save a request, or guarantee that
an action can run immediately. Calling the action still acquires its normal
locks, checks the latest persisted state and rejects conflicting unfinished
work. Applications must handle a later refusal without damaging existing data.

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
The `django-logic:` prefix is reserved for engine-created successors and effects.
The database job ID remains stable across retries. Existing provider request IDs can remain in application records.
**Cleanup retains records with public request keys indefinitely.**
The application must choose when deleting those records is safe. Deletion ends duplicate-submission protection.
Public keys are opt-in: use one when repeated submissions must resolve to the
same job. An ordinary background submission does not require a request key.
Records without request keys, and completed jobs with known internal successor
or effect keys, use the configured retention period. Cleanup keeps unfinished
jobs and the newest failed parent or required job for each process. Completed
independent effects, including failures, expire normally because they cannot
change the parent state. Internal jobs are saved atomically with parent completion; a repeated public parent request
still returns its retained parent record and cannot recreate expired follow-ups.

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

## Measurements retained after completion

Each job retains `first_started_at` and `start_attempt_count`. The worker saves them
before trying to execute the job, together with the existing `started_at` value.
They survive an attempt rollback or a job process crash. A job already locked
at this check, or waiting for a future `eligible_at`, does not increase the count.
The worker releases this short lock before taking the lock used during execution.
Two workers can therefore record a start before only one executes the job.
`start_attempt_count` counts these saved start markers, not executed side effects
or provider requests. `first_started_at` is the first recorded start attempt,
which can precede the worker that actually performs the work.

Every recorded `Defer` increases `deferral_count` without increasing
`errors_count`. The worker also saves `last_deferred_at`,
`last_deferred_until` and `last_deferral_reason`. These remain after a retry
or completion, while `eligible_at` and `deferral_reason` still describe only
the current wait. The two retained times describe the last requested wait,
not all elapsed waiting. Applications should use a short reason without
credentials or request payloads.

For jobs created after the upgrade, `first_started_at - created` measures
the delay until the first recorded start attempt. That delay includes any initial
application-specified eligibility time. `completed_at - created` includes
execution, errors, deferrals and time waiting for a worker. `duration_ms`
still measures only the last attempt and excludes completion callbacks.

These measurements begin with the first attempt on an upgraded worker.
Migration 0014 leaves old start and wait history unknown instead of guessing.
Apply the migration before starting upgraded workers. Its nullable timestamps
and database defaults let older 3.x producers continue inserting jobs.
Older workers do not update these measurements, so mixed-worker totals are
incomplete. Existing retention rules still apply to the job records.

Essential side-effect error logs identify the failing callable, action,
model record and exception type. This separates business work failures from
best-effort callback failures without logging the callable's arguments.

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

## Independent background effects

Use an independent effect for work that must be submitted reliably but whose
completion is not required for the parent operation to finish:

```python
from django_logic.background import BackgroundEffect, BackgroundTransition

BackgroundTransition(
    "submit", sources=["new"], target="submitted", atomic=False,
    side_effects=[submit_order_and_save_confirmation],
    background_effects=[
        BackgroundEffect("annotate", add_optional_annotation,
                         queue="shared", atomic=False, timeout=300),
    ],
)
```

The parent completion, final state and every effect job are saved in one
transaction. If saving any effect fails, none of that completion commits;
the parent retries. Previously committed external confirmations therefore still
need receipts or provider idempotency. A failed, cancelled, superseded or deferred
parent creates no effects. Each effect has a stable request key derived from the
parent job and effect name; retries cannot create another copy of that effect.

Effects use the same workers and job table. They inherit the parent's fairness
key and saved arguments, with a new trace ID and the parent trace as `parent_id`.
The default queue is the parent's recorded queue. `timeout`, `atomic` and
`no_retry_on` have the same meanings as for a background transition; timeout
defaults to no limit and atomic execution defaults to true. `Defer` releases the
worker slot normally. Multiple effects are independent and may execute in any
order, concurrently, or concurrently with a later parent operation.

An effect never owns the parent's unfinished-job allowance. Its success,
failure, timeout or cancellation does not write the parent's state or run the
parent's callbacks. Later state changes do not automatically cancel it. The
callable must check whether its work still applies, and must use appropriate
receipts, application locks or idempotency for its own writes. Independence does
not imply that arbitrary edits or cancellation of the underlying business
record are safe. Those policies belong to the application.

Only background parents are supported. The model and jobs must use the default
database for reads and writes; cross-database routing is refused. Non-atomic
work additionally requires PostgreSQL. In sync mode, effect execution is
scheduled with `transaction.on_commit`: autocommit callers run it before the
parent call returns; an outer transaction delays it until commit, and rollback
runs none. Every effect is attempted even when an earlier effect fails; effect
errors remain on the child jobs and do not raise an error for the successful
parent. Explicitly running a child in sync mode retains the normal exception
behavior. `TestCase` callers can use `captureOnCommitCallbacks(execute=True)`;
non-atomic tests should use `TransactionTestCase` and PostgreSQL.

`background_effect_names` freezes the parent's effect names when it is submitted.
Adding a declaration later does not add work to an existing job. Removing a
frozen declaration prevents parent completion; removing an effect or its exact
declaring process after child creation records a visible failure without running
another declaration. Preserve names, owning classes and input compatibility
until outstanding jobs finish. Callables are resolved from deployed code, not
serialized; compatible code fixes apply to pending jobs.

Effect rows expose `effect_name`, `parent_transition_name` and `parent_job_id`.
Their reserved `transition_name` cannot resolve to the parent's action in an
older worker. `operation_name`, `dl_transitions` and monitoring show the readable
parent-action/effect name. `for_instance()` returns parents and effects;
`in_flight_for()` and `in_flight()` concern only state-owning parent jobs.
Find effects with `TransitionMessage.objects.filter(parent_job_id=parent.pk)`.
`cancel_pending()` can cancel one waiting effect without cancelling siblings or
its parent; running work is not interrupted. Completed effects use the configured
retention period for both success and failure. Their failures do not change the
parent state and have no indefinite retention exception. Parent IDs remain recorded if normal cleanup removes an unkeyed parent. Retrying a completed parent
does not recreate effects that expired. Public request keys remain retained.

`ProcessScenario`'s transition assertions select the parent job; its side-effect
assertions concern the parent's essential callables. Inspect effect jobs
separately. The existing single-job snapshot saves the parent's frozen effect
names but does not capture independent child history or a related object graph.

### Upgrading to independent effects

Apply migration 0015 before new producers or workers start. Stop all earlier
workers before code can create effects, then start only upgraded workers.
Earlier producers can still insert parent rows using database defaults, but
those rows have an empty effect-name snapshot. When replacing an old callback,
keep an explicit compatibility path for those outstanding parents; otherwise
their old follow-up work would disappear. Do not run both that legacy submission
and a new declared effect for the same operation.

The reserved child action prevents an earlier worker from replaying parent
side effects; it does not make mixed worker versions supported. An earlier
worker could mark an effect failed as an unknown action. Before rolling back to
earlier workers, stop new submissions and finish or explicitly resolve every
pending effect. Do not reverse migration 0015 while independent jobs remain
unfinished. This feature reuses the existing workers and introduces no scheduler.

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

Completing a failed job makes a bounded check of the business row before writing
`failed_state`. If the state changed, the job completes as `superseded` and skips
failure callbacks. If another transaction still holds the row, or its state cannot
be checked, the job completes
as failed, records why the failed state could not be written, and leaves the
business state unchanged without running failure callbacks. A final attempt can
therefore make two bounded lock attempts: one before its work and one before
completion. Normal retries keep their existing error count and pause. The same
completion check applies to `atomic=False` jobs and stuck-job completion; a
non-atomic job keeps any provider confirmations already committed.

```python
from django_logic.background import cancel_pending
result = cancel_pending(job_id)
```

Results are `cancelled`, `running`, `finished`, or `missing`.
The function does not wait for a running job. It does not change business state or invoke callbacks.
Applications remain responsible for their cancellation state and any earlier confirmed external effects.
An unfinished job between attempts can be cancelled. This is not a promise to interrupt an active provider request.

## Upgrade and rollback

Migration 0011 in 3.0.1 keeps database defaults on the new job columns, so earlier producers can still insert jobs afterwards.
Migration 0013 restores these defaults for databases that already applied the published 3.0.0 migration.
Schema changes can briefly block database queries; compatible inserts do not guarantee an uninterrupted deployment.

Workers from before 3.0 ignore deferred eligibility, required successors and fairness. Their cleanup also does not preserve request-key records.
Database defaults do not make it safe to run those workers alongside upgraded code. When upgrading from before 3.0, use this order:

1. Stop old workers from taking new work and let their active jobs finish. Include application-specific job runners and maintenance commands, not only `dl_worker`. Keep queued jobs.
2. Stop those workers before upgraded producers start submitting work. Apply the migrations while worker counts remain zero.
3. Upgrade the web processes and other producers. Compatible earlier web processes may keep accepting requests and queuing jobs during preparation, provided they do not execute jobs inline and the application has verified that its existing queued jobs still run correctly. Confirm that the old processes have finished; deployment platforms can temporarily overlap versions.
4. Start the new workers after the application upgrade is complete. Check the installed version, queue coverage and completion of existing queued jobs.

A rollback to a version before 3.0 needs a review of application state and saved job records after new work has run.
Those workers cannot honor deferred eligibility, required follow-ups or request-key retention, including completed records that protect against duplicate requests.
Leaving the extra columns in place is not sufficient to preserve those behaviors.

Existing job rows receive neutral defaults and preserve their previous identities and inputs.
The fairness migration creates shared cursor records for their existing group keys.
The operator output distinguishes normal waiting from retrying after an error.

Subclasses overriding `_enqueue_atomic` must accept and forward its new keyword arguments:
`submission`, `using`, `request_key`, and `fairness_key`.
The internal required-successor writer must retain the supplied database alias.
Calling the public transition method from inside completion is not a substitute.
