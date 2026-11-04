# Durable operations

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

## Fair progress

Declare `fairness_key="group"` or a callable accepting `instance, **kwargs`.
The callable returns a string, for example `f"client:{instance.client_id}"`.
The engine saves the key with the job and preserves it across retries.
Workers take turns between eligible fairness groups using shared database records.
A single group can use every free worker slot. Existing running work is not interrupted when another group arrives.
The empty key groups legacy submissions together. Queues still select which workers may run a job.
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
If another submission already saved unfinished work, the engine discards the
losing preparation's writes and callbacks, then raises `AlreadyInProgress`.
Do not suppress errors or mark the transaction for a silent rollback.

Keep the context short and local: no provider calls or other irreversible work.
After-commit callbacks are appropriate for best-effort external history. They
do not make that history transactional or recover it after a caller crash.
Use durable database records when that distinction matters.

The context ends before job execution, including inline execution. An outer
caller transaction can still retain its database locks until commit.
Retries and previously saved jobs do not run the context again. Jobs keep the
existing serialized argument format. Independent effects receive the saved
inputs of their parent job.

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

Essential side-effect error logs identify the failing callable, action,
model record and exception type. This separates business work failures from
best-effort callback failures without logging the callable's arguments.

## Required follow-up

`callbacks` and `next_transition` are best-effort. A crash can lose them.
Keep required work in a job's side effects, or submit application-owned jobs
and record their completion in business records.

### Required work on related records

An order can reach `shipping` before its old callback ships the fulfilments.
A manifest must not treat that state change as permission to finish billing.
Required shipping work belongs in a durable order job with a separate completion state:

```python
from django_logic import Process
from django_logic.background import BackgroundEffect, BackgroundTransition


class OrderShippingCompletion(Process):
    process_name = "shipping_completion_process"
    transitions = [
        BackgroundTransition(
            "complete", sources=["not_requested", "failed"],
            in_progress_state="processing", target="completed", failed_state="failed",
            atomic=False,
            side_effects=[complete_shipping_and_verify_results],
            background_effects=[
                BackgroundEffect("notify", notify_shipping_complete),
                BackgroundEffect("inventory", publish_inventory),
                BackgroundEffect("manifest", refresh_manifest),
            ],
        ),
    ]
```

Bind this process to the order's `shipping_completion_state` in `AppConfig.ready()`.
In pull mode, save the accepted shipping change and submit `order.shipping_completion_process.complete()` in one transaction.
The worker runs the saved job after that transaction commits.
The required callable ships fulfilments, verifies their results and applies shipping rates.
Required failures must raise errors; legacy helpers that swallow errors need explicit result checks.
`completed` then records business success even after normal job cleanup.
Absence of an unfinished job does not establish success.

Use `atomic=False` when existing fulfilment work commits progress or publishes events independently.
A later rate failure must not roll back shipping that has already been announced.
Retries preserve confirmed child progress and resume only unfinished work.
The order reaches `completed` only after every required result is verified.
Default atomic execution suits required database work whose external publication waits for commit.

Inline execution of `atomic=False` work requires autocommit.
Tests of transactional submission must submit in pull mode, commit, then call `retry_pending()` to execute saved jobs inline.
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
Independent publication, notifications and refreshes can use `BackgroundEffect`; they must not decide whether required shipping succeeded.
Save computed values needed by later jobs in business records.
Mutating execution arguments does not pass new values to saved jobs or effects.

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
need receipts or provider idempotency. A failed, superseded or deferred
parent creates no effects. Each effect has a stable request key derived from the
parent job and effect name; retries cannot create another copy of that effect.

Effects use the same workers and job table. They inherit the parent's fairness
key and saved arguments, with a new trace ID and the parent trace as `parent_id`.
Changes to nested arguments during parent execution do not change these saved inputs.
The default queue is the parent's recorded queue. `timeout` and `atomic`
have the same meanings as for a background transition; timeout
defaults to no limit and atomic execution defaults to true. `Defer` releases the
worker slot normally. Multiple effects are independent and may execute in any
order, concurrently, or concurrently with a later parent operation.

An effect never owns the parent's unfinished-job allowance. Its success,
failure or timeout does not write the parent's state or run the
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
Completed effects use the configured retention period for success and failure.
Their failures do not change the parent state and have no indefinite retention exception.
Parent IDs remain recorded if normal cleanup removes the parent.
Do not reopen completed jobs by editing their rows.
If a reopened parent reaches completion again, retained effect keys reject duplicates; after cleanup, those keys no longer provide protection.
A state-changing parent may instead stop as superseded before creating effects.
These records do not provide permanent duplicate protection for external work.
Effect functions must check whether their work still applies before contacting a provider.


`ProcessScenario`'s transition assertions select the parent job; its side-effect
assertions concern the parent's essential callables. Inspect effect jobs
separately. Model snapshots capture the model fields only. Use explicit job
fixtures to reproduce pending work; snapshots do not copy background job history.

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
For a lock-taking synchronous `Transition`, callbacks and `next_transition` also run inside the PostgreSQL transaction, before the outermost commit.
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
It does not interrupt a provider request already running or cancel independent effects.

## Upgrade and rollback

Version 4 removes unused public request keys, required successors and pending-job
cancellation. Applications must remove calls to these APIs before upgrading.
Verify that unfinished jobs have empty `required_next` values. Applications that
used public request keys must resolve their duplicate-protection policy first.

Migration 0016 changes Django's model state only. The physical columns and their
defaults remain, so 3.3 processes can insert and read jobs during deployment.
The saved arguments, fairness keys, deferral times and independent effects keep
their existing format. Applications must preserve their own callable names and
saved inputs while old and new processes overlap.

For a 3.3 application that meets these conditions, apply migrations and deploy
compatible producers and workers through the normal rolling release. Check the
installed version, queue coverage and completion of previously queued jobs.
Rollback to 3.3 preserves this schema and these job formats. Application changes
still need their own rollback review.

Databases on 0.17.x or earlier must first run `migrate` with Django Logic 3.2.
The current package keeps the squashed migration's replacement history.
Upgrades from before 3.0 require different worker ordering; follow the
[earlier upgrade sequence](https://github.com/Borderless360/django-logic/blob/v3.0.1/docs/operation-contract.md#upgrade-and-rollback).
