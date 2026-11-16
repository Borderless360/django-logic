# Background jobs

A `BackgroundTransition` saves accepted work in the database. Workers run its functions and retry failed attempts.
The business record shows the accepted, completed or failed state; the job records attempts and waiting.
See the [declaration reference](reference.md#transition-declarations) for options and defaults.

## Submitting a job

Call the declared action with business inputs and, for a person's request, `user=`:

```python
order.process.ship(parcel_ids=[10, 11], user=user)
```

The call validates the request and saves the job with any declared in-progress state.
Accepted work is not completed work. A worker can start the job after its submission transaction commits.
Only one unfinished job can exist for a record and process.
`AlreadyInProgress` means the new request was not accepted.
Applications that combine refresh requests must save their own requested version or dirty flag.

## Preparing a submission

Use `submission_context` when accepting work must also lock related records,
recheck business conditions or save trusted inputs.

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
Retries and previously saved jobs do not run the context again. Each job receives its own saved inputs.

## Expected waiting

```python
from datetime import datetime, timedelta, timezone
from django_logic.background import Defer

raise Defer(
    until=datetime.now(timezone.utc) + timedelta(seconds=retry_after),
    reason="provider allowance",
)
```

The worker saves `eligible_at` and `deferral_reason`, then can start another job.
The supplied deadline must include a timezone. With `USE_TZ=False`, the worker converts it to Django's default timezone for storage.
It does not increment `errors_count`, write a failed state, or run completion callbacks.
The next attempt starts from the beginning. With default atomic execution, the attempt's database writes roll back.
With `atomic=False`, previously committed receipts survive. Both modes still require idempotent external requests.
Direct execution and `retry_pending()` also respect `eligible_at`.
The operator's `--send` command refuses to override a future deferral.
`retry_status()` treats deferred jobs as waiting through `eligible_at`, with the normal retry window allowed for pickup afterward.

## Required follow-up

`callbacks` are best-effort. A crash can lose them.
Keep functions in one job when they share a completion result.
For a separate result, save its business record and required job in one database transaction.
A stopped process then leaves both records committed, or neither.

### Required work on related records

An order has one or more fulfilments. A manifest groups orders, and billing charges for their confirmed shipping.
An order can reach `shipping` before all its fulfilments finish shipping.
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

See [tests with real commits](TESTING_GUIDE.md#tests-with-real-commits) for transactional submission and inline execution.

The manifest submits bounded per-order jobs and lets workers process orders independently.
It reads the saved completion states and required fulfilment results before billing.
It raises `Defer` while orders are processing and reports failed orders for recovery.
Save those order submissions before deferring: a default atomic attempt rolls its writes back on `Defer`.
An `atomic=False` manifest job can use short application transactions for each order submission.
Retries must recognize accepted or completed shipping work instead of submitting it again.
Billing records completion only after its required charges succeed.

Save computed values needed by later jobs in business records.
Mutating execution arguments does not change inputs already saved for another job.

### Separate business results

Declare a separate background transition when work has its own business result.
For example, completing an import and delivering an invoice email are named application operations.
Their functions contain business rules. Their declarations choose queues, timeouts and transaction behavior.
They use the same saved jobs and worker as every other background transition.

If a related job must wait for its parent, it checks the saved business result and raises `Defer` while that result remains incomplete.
Its next attempt runs after its due time; inline test execution does not repeatedly run deferred jobs.

A required parent check must also have its own saved job.
Submit that check with its child jobs before any child completion is needed.
See [work on related records](recipes/related-records.md).
For work that can share one completion result, keep its functions in the same job.
For paged work, one job can save progress and defer its next attempt.
See [long imports](recipes/long-jobs.md).

A job checks its declared source state before each attempt and before completion.
A later business state can make old work unnecessary.
Application functions must still verify accepted inputs and confirmed provider results before repeating external changes.
A queue separates execution priority; it does not change these business rules.
See [interactive and automatic requests](recipes/interactive-and-automatic.md).

Finish outstanding jobs before changing their recorded action, process or field.
The worker uses the exact saved identity and never substitutes a similarly named declaration.

## Application context

An application can preserve audit attribution or tracing across worker processes with `DJANGO_LOGIC['JOB_CONTEXT']`.
Set it to the dotted path of an adapter class. This is application configuration, not a transition option.
Business functions and declarations need no additional arguments.

The library constructs the adapter without arguments. `capture()` returns JSON-compatible data or `None` when a job is submitted.
The library saves an independent JSON copy in `TransitionMessage.job_context`, separately from business arguments.
Convert values such as UUIDs, dates and decimals in the adapter; capture does not use the broader saved-argument serializer.
Capture failures refuse submission; retries use the originally saved context rather than the retrying person's context.

For each attempt, `restore(value)` returns a context manager. Its scope includes required functions, optional callbacks and failure handling.
It also applies when periodic maintenance completes an exhausted job and when `atomic=False` is selected.
The adapter must restore the previous ambient context when its block exits, including after an error.
Jobs without saved context call `restore(None)`; the adapter must clear attribution instead of inheriting the worker's ambient user.

If context setup fails, the worker records the error and applies the normal retry allowance.
After that allowance, the job completes as failed with `[unrestorable]`.
Business functions and callbacks do not run when context setup fails.
The business record keeps its current state, including a declared in-progress state.
Maintenance handles jobs that have already used their error allowance. A context setup error there completes the job immediately, without another retry allowance.
The job retains its earlier attempt error alongside the context error.
Inspect both errors, correct the adapter and reconcile any uncertain provider result before requesting the action again.
A declared `in_progress_state` is also an allowed source for that action.
Context cleanup cannot suppress a business error or cause committed work to run again.
Cleanup errors are logged separately, including during inline execution; they do not consume another job retry.

```python
from contextlib import contextmanager
from contextvars import ContextVar

current_origin = ContextVar('current_origin', default=None)


class JobContext:
    def capture(self):
        return current_origin.get()

    @contextmanager
    def restore(self, value):
        token = current_origin.set(value)
        try:
            yield
        finally:
            current_origin.reset(token)


DJANGO_LOGIC = {'JOB_CONTEXT': 'myapp.audit.JobContext'}
```

The application validates and assigns trusted origin information before calling a business action.
Do not copy an HTTP request, credentials or arbitrary client-provided identity into this context.
Context supports attribution; it does not grant permission or replace saved business inputs.

## Fair progress

Declare `fairness_key="group"` or a callable accepting `instance, **kwargs`.
The callable returns a string, for example `f"client:{instance.client_id}"`.
The engine saves the key with the job and preserves it across retries.
Workers take turns between eligible fairness groups using shared database records.
A single group can use every free worker slot. Existing running work is not interrupted when another group arrives.
The empty key groups submissions without an application fairness key together. Queues still select which workers may run a job.
Fair selection does not enforce provider request limits or guarantee equal completion times.

Use a small, stable set of keys, such as one key per account rather than one per record.
The library retains fairness-group records; completed-job cleanup does not delete them.

## Measurements retained after completion

Each job retains `first_started_at` and `start_attempt_count`. The worker saves them
before trying to execute the job, together with `started_at`.
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

`first_started_at - created` measures
the delay until the first recorded start attempt. That delay includes any initial
application-specified eligibility time. `completed_at - created` includes
execution, errors, deferrals and time waiting for a worker. `duration_ms`
measures only the last attempt and excludes completion callbacks.

Missing historical measurements remain unknown. Cleanup limits the available job history.

`transition_statistics(since=cutoff, queues=None, using=None)` groups retained
jobs by queue, model, process class and action. Counts describe each
selected job's recorded history, not events that happened within the window.
`initial_wait_ms` uses only `first_started_at`; `last_attempt_duration_ms` uses
only recorded `duration_ms` values. Each contains `samples`, `p50`, `p90` and
`max`. Percentiles use the nearest rank. Missing measurements are excluded.
`repeated_starts` includes deferrals and competing recorded starts. It is not an
exact retry count. These records cannot establish total runtime or utilization.
`dl_transitions --stats --since 7d --json` exposes the same report without
changing jobs.
