# Durable operations

## Checking eligibility while another action runs

`process.get_available_actions(..., include_locked=True)` and
`process.get_available_transitions(..., include_locked=True)` check the object's
current state, conditions and permissions even when another action holds its
state lock. This lets an application distinguish a temporary wait from a business
rule that forbids an action. The default continues to hide actions that need a
held lock.

Inside `DatabaseTransition`, the lock is visible only within the current call.
Other connections can list those actions, but calls still wait for the PostgreSQL row lock or refuse after its timeout.

These queries do not acquire or release locks, save a request, or guarantee that
an action can run immediately. Calling the action still acquires its normal
locks, checks the latest persisted state and rejects conflicting unfinished
work. Applications must handle a later refusal without damaging existing data.

## Fair progress

Declare `fairness_key="group"` or a callable accepting `instance, **kwargs`.
The callable returns a string, for example `f"client:{instance.client_id}"`.
The engine saves the key with the job and preserves it across retries.
Workers take turns between eligible fairness groups using shared database records.
A single group can use every free worker slot. Existing running work is not interrupted when another group arrives.
The empty key groups submissions without an application fairness key together. Queues still select which workers may run a job.
Fair selection does not enforce provider request limits or guarantee equal completion times.

The existing one-unfinished-job rule per object and process remains.
`AlreadyInProgress` means the new request was **not accepted**.
Applications that combine refresh requests must persist their own dirty flag or requested version.

## Preparing a submission

Use `submission_context` when accepting work must also lock related records,
recheck business conditions or save trusted inputs. It replaces a private
`_enqueue_atomic` override for these application rules.

```python
from contextlib import contextmanager
from django.db import transaction
from django_logic.background import BackgroundTransition


@contextmanager
def prepare_shipping(transition, state, kwargs, *, using):
    order = Order.objects.using(using).select_for_update().get(pk=state.instance.pk)
    check_shipping_allowed(order)
    kwargs["parcel_ids"] = list(order.parcels.using(using).values_list("pk", flat=True))
    yield
    transaction.on_commit(lambda: record_shipping_request(order.pk), using=using, robust=True)


BackgroundTransition(
    "ship", sources=["ready"], target="shipped",
    in_progress_state="shipping", submission_context=prepare_shipping,
    side_effects=[ship_saved_parcels],
)
```

The callable returns a synchronous context manager. The engine gives it the
transition, bound `State`, a detached argument dictionary and the database alias.
Add, replace or remove ordinary application arguments before `yield`. The
engine serializes those arguments, creates the job and writes its in-progress
state before the context exits. The context cannot change the bound record,
process or engine-managed user and trace arguments.

Preparation and its database writes use the transaction that saves the job.
Use the supplied `using` alias for queries and `transaction.on_commit` callbacks.
A refusal, serialization error or failed submission rolls those writes back.
If another submission already saved unfinished work, the engine raises
`AlreadyInProgress` before running preparation.
Do not suppress errors or mark the transaction for a silent rollback.

Keep the context short and local: no provider calls or other irreversible work.
After-commit callbacks are appropriate for best-effort external history. They
do not make that history transactional or recover it after a caller crash.
Use durable database records when that distinction matters.

The context ends before job execution, including inline execution. An outer
caller transaction can still retain its database locks until commit.
Retries and previously saved jobs do not run the context again. Jobs keep the
existing serialized argument format. Each job receives its own saved inputs.

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
Existing retention rules still apply to the job records. Missing historical
measurements remain unknown; they are not reconstructed at upgrade.

`transition_statistics(since=cutoff, queues=None, using=None)` groups retained
jobs by queue, model, process class, action, parent action and effect. Counts describe each
selected job's recorded history, not events that happened within the window.
`initial_wait_ms` uses only `first_started_at`; `last_attempt_duration_ms` uses
only recorded `duration_ms` values. Each contains `samples`, `p50`, `p90` and
`max`. Percentiles use the nearest rank. Missing measurements are excluded.
`repeated_starts` includes deferrals and competing recorded starts. It is not an
exact retry count. These records cannot establish total runtime or utilization.
`dl_transitions --stats --since 7d --json` exposes the same report without
changing jobs. Cleanup limits the available history.

Synchronous side-effect errors identify the failing callable, action, model record and exception type.
Background failure logs identify the action, model record, saved job and exception type; the traceback locates the failure.
These logs exclude callable arguments and distinguish required work failures from best-effort callback failures.

## Required follow-up

`callbacks` are best-effort. A crash can lose them.
Keep required work in a job's side effects, or submit application-owned jobs
and record their completion in business records.

### Required work on related records

An order can reach `shipping` before its old callback ships the fulfilments.
A manifest must not treat that state change as permission to finish billing.
Required shipping work belongs in a durable order job with a separate completion state:

```python
from django_logic import Process
from django_logic.background import BackgroundTransition


class OrderShippingCompletion(Process):
    process_name = "shipping_completion_process"
    transitions = [
        BackgroundTransition(
            "complete", sources=["not_requested", "failed"],
            in_progress_state="processing", target="completed", failed_state="failed",
            atomic=False,
            side_effects=[complete_shipping_and_verify_results],
        ),
    ]
```

Bind this process to the order's `shipping_completion_state` in `AppConfig.ready()`.
In pull mode, save the accepted shipping change and submit `order.shipping_completion_process.complete()` in one transaction.
The worker runs the saved job after that transaction commits.
The required callable ships fulfilments, verifies their results and applies shipping rates.
Required failures must raise errors; application helpers that swallow errors need explicit result checks.
`completed` then records business success even after normal job cleanup.
Absence of an unfinished job does not establish success.

Use `atomic=False` when existing fulfilment work commits progress or publishes events independently.
A later rate failure must not roll back shipping that has already been announced.
Retries preserve confirmed child progress and resume only unfinished work.
The order reaches `completed` only after every required result is verified.
Default atomic execution suits required database work whose external publication waits for commit.

Inline execution of `atomic=False` work requires autocommit, except in the test runtime.
There, a job called inside a transaction runs in that transaction and keeps its writes when a later step fails.
Tests of transactional submission must submit in pull mode, commit, then call `retry_pending()` to execute saved jobs inline.
Its return value counts calls without raised exceptions, including failed attempts in pull mode.
Check saved job and business results to establish success.
Use `TransactionTestCase` for those real commits.
Do not add an empty production job just to make transactional submission run inside a synchronous test.

The manifest submits bounded per-order jobs and lets workers process orders independently.
It reads the saved completion states and required fulfilment results before billing.
It raises `Defer` while orders are processing and reports failed orders for recovery.
Save those order submissions before deferring: a default atomic attempt rolls its writes back on `Defer`.
An `atomic=False` manifest job can use short application transactions for each order submission.
Retries must recognize accepted or completed shipping work instead of submitting it again.
Billing records completion only after its required charges succeed.

Keep required work in one job's `side_effects` when it shares one unit of completion.
Related models use their own ordinary background jobs and application-owned completion checks, as above.
Required publication, notifications and refreshes use ordinary application jobs when they have their own completion result.
Save computed values needed by later jobs in business records.
Mutating execution arguments does not change inputs already saved for another job.

## Separate business operations

Declare a separate ordinary background transition when work has its own business result.
For example, completing an import and delivering an invoice email are named application operations.
Their functions contain business rules. Their declarations choose queues, timeouts and transaction behavior.
They use the same saved jobs and worker as every other background transition.

Save a required job with the business result that requests it, inside one database transaction.
A stopped process then leaves both records committed, or neither.
Do not submit required work only from an optional callback.
If a related job must wait for its parent, it checks the saved business result and raises `Defer` while that result remains incomplete.
Its next attempt runs after its due time; inline test execution does not repeatedly run postponed jobs.

A required parent check must also have its own saved job.
Submit that check with its child jobs before any child completion is needed.
See [parent and child processes](recipes/nested-processes.md).
For work that can share one completion result, keep its functions in the same job.
For paged work, one job can save progress and postpone its next attempt.
See [long imports](recipes/long-jobs.md).

An ordinary job checks its declared source state before each attempt and before completion.
A later business state can make old work unnecessary.
Application functions must still verify accepted inputs and confirmed provider results before repeating external changes.
A queue separates execution priority; it does not change these business rules.
See [interactive and automatic requests](recipes/interactive-and-automatic.md).

`ProcessScenario` selects ordinary jobs for the bound process.
Model snapshots contain model fields, not job history.
Use explicit job fixtures to reproduce pending work.

Finish outstanding jobs before changing their recorded action, process or field.
The worker uses the exact saved identity and never substitutes a similarly named declaration.

## State protection

On PostgreSQL, lock-taking transitions acquire the physical business-row lock.
They wait at most two seconds for another transaction that holds the row.
A holder that outlasts the wait causes `TransitionTemporarilyUnavailable` before application work starts, and the engine logs a warning that names the record.
A background job that meets this refusal records an error and retries after the normal pause. It does not wait quietly.
The database retains the lock through the actual outer commit or rollback.
Inside this transaction, synchronous side effects run in a savepoint. A database error in a side effect discards their writes and still records the failed state and runs the failure callbacks. Any other error keeps the writes made before it.
The shared cache lock also prevents overlapping external calls while its timeout
remains valid, including after a lost database connection. PostgreSQL protects
database state; neither lock can undo an external request already sent.
Saving a background job calls no provider. On PostgreSQL that save holds the business row and writes no cache lock.
It still refuses a record that has a cache lock. A process that stops during the save leaves no lock on the record.
For a lock-taking synchronous `Transition`, callbacks also run inside the PostgreSQL transaction, before the outermost commit.
Each one runs in a savepoint. A database error discards its writes; any other error keeps the writes made before it, including the failed state of a transition it started.
The cache lock is released first, so callbacks can call another transition on the same instance.
Other connections still see the prior committed state. An interruption can discard the target and side-effect writes before commit.
Pull workers run background success callbacks after the job commits; a crash can lose callbacks after completion.
Inline background execution can still be inside a caller's outer transaction.
Use `transaction.on_commit()` for best-effort publication that requires committed data, not as a guarantee of required work.

On other databases, `State.protected()` takes no row lock and opens no transaction.
Without a caller transaction, synchronous writes commit independently and swallowed database errors may not be detected.
SQLite remains useful for synchronous tests, but does not verify PostgreSQL transaction or concurrent-state protection.

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

Applications own cancellation state and any confirmed external effects.
A state change supersedes an unfinished state-changing job before its next attempt.
It does not interrupt a provider request already running or undo a confirmed external change.

## Upgrade and rollback

Version 5 is a breaking release. Its supported upgrade baseline is 4.3.2.
Earlier installations must complete their earlier upgrade steps first, using
the corresponding release documentation. Historical migration files remain so
Django can recognize applied migrations; they do not provide an older runtime.

Before upgrading:

1. Remove every `next_transition` declaration and `NextTransition` import.
   Put required work in saved jobs. Use an ordinary callback only when its
   failure or loss is acceptable; replacing the name alone does not make work durable.
   Remove `BackgroundEffect` and `background_effects` declarations too.
   Move required functions into their owning job, or save ordinary business jobs with the result that requests them.
2. Make injected `ProcessScenario` failures explicit with `expect_raises=ErrorType`
   or `expect_raises=False`. Omitting it no longer absorbs the injected exception.
3. Keep root process classes importable and preserve their `process_name`, bound
   field, owning class and action names while jobs using them remain unfinished.
   Submit background jobs through a `Process` to record all identity fields.
   Low-level `change_state` calls must supply the same complete, valid identity. Attribute lookup, renamed-class and action-name fallbacks are removed.
4. Finish or explicitly resolve unsupported unfinished jobs on 4.3.2, including
   jobs with `required_next` instructions or incomplete identity. Do not mark work
   complete merely to pass the migration. When application declarations are removed,
   finish their jobs before installing that application change.
   Finish all pending background effects and parents that still promise them using the installed version.
5. Stop all job producers, scheduled submissions and old workers. Confirm that no
   old job process remains. Back up the database, install the new application and
   run `migrate` before starting any 5.0 process.

Migration 0017 checks unfinished work before removing the physical
`request_fingerprint` and `required_next` columns. A failed check leaves the
columns and jobs intact. Resolve the reported work using the previous application,
then retry. The migration does not call providers, replay jobs or rewrite identities.
A bounded PostgreSQL lock wait can also stop the migration; retry after the holder
finishes, with producers still stopped.

Migration 0018 refuses unfinished jobs with background-effect metadata or a nonempty request key.
It then removes `request_key`, `background_effect_names`, `effect_name`, `parent_transition_name` and `parent_job_id`.
Compatible ordinary pending jobs remain available and completed job rows remain in history.
The backup preserves the removed metadata; reversing the migration cannot restore those values.

Start only 5.0 producers and workers. Verify the installed package, queue coverage
and completion of retained jobs. Mixing earlier workers or producers with 5.0 is
unsupported. This release is not a normal rolling upgrade.

Before rollback, stop new submissions and workers and review every unfinished job
against the previous application's declarations. Reversing 0017 and 0018 recreates empty
obsolete columns; it cannot restore removed values or reverse application changes.
Do not start the previous application until its schema and saved jobs are compatible.
