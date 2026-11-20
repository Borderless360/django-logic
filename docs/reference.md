# API and settings reference

Django Logic declares which business actions are available and saves jobs that workers can retry.
Business rules belong in application functions. Worker configuration belongs in deployment settings.

## Imports

| Import | Purpose |
| --- | --- |
| `from django_logic import Process, ProcessManager` | Declare a process and bind it to a model field. |
| `from django_logic import Transition, DatabaseTransition` | Run a synchronous action; the database-only class requires PostgreSQL. |
| `from django_logic.background import BackgroundTransition` | Save an action for a worker. |
| `from django_logic.background import Defer, PermanentFailure` | Wait until a specified time, or report a failure that retrying cannot fix. |
| `from django_logic.background import sync_execution` | Run background work inline within one block. |
| `from django_logic.background import in_flight, transition_statistics, queue_worker_presence` | Read job and worker observations. |
| `from django_logic.background import AlreadyInProgress` | Handle a refused duplicate submission. |
| `from django_logic.exceptions import TransitionNotAllowed, TransitionTemporarilyUnavailable, RefusalReason` | Handle unavailable actions. |
| `from django_logic.commands import StrictPermissions, NoUserPermissions, refusal_message` | Require a person, require automation, or explain a failed business check. |
| `from django_logic.testing import ProcessScenario` | Test a business process inline. |
| `from django_logic.testing import snapshot, from_snapshot, open_transition_message` | Capture model fields, rebuild a test record, or create an unfinished job fixture. |
| `from django_logic.testing import assert_idempotent` | Call a function twice and compare the selected saved fields and observations. |
| `from django_logic.background import retry_pending` | Run each eligible saved job once inline. |
| `from django_logic.background.runner import run_background_transition` | Run one eligible saved job by its identifier. |
| `from django_logic.background.models import TransitionMessage` | Read a saved job and its recorded outcome. |
| `from django_logic.background.serializers import KwargsSerializationError` | Handle an argument that cannot be saved or restored. |
| `from django_logic.background.context import JobContextError` | Handle application-context restoration failure during inline execution. |
| `from django_logic.conf import enable_sync` | Allow deployment-wide inline execution from test settings before Django starts. |

## Transition declarations

`Transition(action_name, sources, target=None, ...)` declares an action that runs in the caller.
`DatabaseTransition` accepts the same options and requires all work to be reversible in the same PostgreSQL database.

| Option | Default | Meaning |
| --- | --- | --- |
| `action_name` | Required | Name used in `instance.process.action()`. |
| `sources` | Required | States from which the action is allowed. |
| `target` | `None` | State saved on success; `None` leaves it unchanged, while `''` saves an empty value. |
| `conditions` | `()` | Each function receives the instance and must return true. |
| `permissions` | `()` | Each function receives the instance and user and must permit that user. |
| `side_effects` | `()` | Required functions, run in declaration order with `instance, **kwargs`. |
| `callbacks` | `()` | Optional functions after success; ordinary exceptions are logged and suppressed. |
| `failure_callbacks` | `()` | Optional failure handling, including the error as `exception=`. |
| `failed_state` | `None` | State saved when failure handling can safely update the record. |
| `refusal_messages` | `{}` | Business explanations indexed by `RefusalReason`. |
| `lock` | `True` | Protect the record's state. `False` cannot be combined with target or failed-state writes. |
| `atomic` | `False` | Roll required database writes and the target save back together on error. |

Background jobs accept those business options and add the following:

| Option | Default | Meaning |
| --- | --- | --- |
| `in_progress_state` | `None` | State saved with the accepted job; requires a target. |
| `queue` | `DEFAULT_QUEUE` | Name of the worker queue. |
| `timeout` | `None` | Positive integer seconds before the worker stops the job process. |
| `atomic` | `True` | Roll back the failed attempt's database writes; use `False` for independently committed provider receipts. |
| `no_retry_on` | `()` | Exception types that end the job immediately as failed. |
| `fairness_key` | `None` | A string or `fn(instance, **kwargs)` that returns the group between which workers take turns. |
| `submission_context` | `None` | A context manager that prepares and saves business inputs in the job submission transaction. |

Background jobs cannot use `lock=False`. The job and its accepted state must be saved together.
See [background jobs](background-jobs.md) for preparation and deferral examples.
See [locks and transactions](transactions.md) before selecting transaction behavior.

## Process declarations

A `Process` class declares `transitions`, `nested_processes`, `conditions`, `permissions` and `refusal_messages`.
Their defaults are empty lists or, for messages, an empty mapping.
`process_name` defaults to `'process'` and names the model accessor.
`permissions_class` defaults to `django_logic.commands.Permissions`.
`state_class` defaults to `django_logic.state.State` and constructs the object that reads and writes the bound field.
Use the provided state implementation unless an application has tested its own locking and persistence contract.

Nested processes share the root process's bound state. Conditions select the available branch:

```python
from django_logic import Process
from django_logic.background import BackgroundTransition
from django_logic.commands import StrictPermissions, NoUserPermissions


class ManualFulfilment(Process):
    permissions_class = StrictPermissions
    transitions = [
        BackgroundTransition(
            'fulfil', sources=['ready'], target='fulfilled',
            in_progress_state='fulfilling', queue='manual',
            side_effects=[fulfil_order],
        ),
    ]


class AutomaticFulfilment(Process):
    permissions_class = NoUserPermissions
    transitions = [
        BackgroundTransition(
            'fulfil', sources=['ready'], target='fulfilled',
            in_progress_state='fulfilling', queue='automatic',
            side_effects=[fulfil_order],
        ),
    ]


class OrderProcess(Process):
    nested_processes = [ManualFulfilment, AutomaticFulfilment]
```

Bind the root once in `AppConfig.ready()`:
`ProcessManager.bind_model_process(Order, OrderProcess, state_field='status')`.
A background action must be unique within its declaring class. Keep that class importable while its jobs remain unfinished.
`nested_processes` groups declarations for one bound state field; it does not create related records or jobs.
For an order waiting on several fulfilment records, use [waiting for work on related records](recipes/related-records.md).

Use the same action name in nested processes when callers share one action API and permissions select the manual or automatic branch.
Use [two explicit action names](recipes/interactive-and-automatic.md) when the endpoint and scheduler select separate actions themselves.
Both choices share the business function and can use separate queues; permission checks must still match each caller.

Use declared functions for application behavior. An `atomic=True` synchronous transition cannot override `complete_transition`.
Its transaction must distinguish required work from optional callbacks.
Custom execution or state overrides must preserve permissions, locks, rollback, failure recording and saved job identity.
They are not a substitute for named business functions.

### Saving a record with a bound process

Creating an order writes its initial state, including when you supply its primary key.
An ordinary save on an existing order writes only its loaded fields outside the process state.
An older copy can therefore save an address change without undoing a completed transition.
Call `refresh_from_db()` to see the current state on that copy.

Transitions name the state field in `update_fields`. You can also name it explicitly for a deliberate correction.
`ProcessManager.state_fields(Order)` returns the names of all bound state fields for the model.
A proxy, parent and child that write the same column share its protection.

On a fully loaded record, an ordinary save keeps `update_fields=None` for `pre_save` and `post_save`.
For a deferred record saved to its original database, Django Logic removes bound state from the field names Django selected.
If that selection is empty, Django skips the save and both signals.
A supplied key that already exists keeps the stored state. Saving to a different database writes the initial state when it inserts a row.
`QuerySet.update()` and `bulk_update()` bypass model saves and can write a state field directly.

## Saved arguments

Pass ordinary business values and `user=`. Do not pass an HTTP request or model instances as business arguments.
Pass model identifiers and load current records when the worker runs.
Conditions receive only the instance; permissions also receive the user.
Required functions and callbacks receive business arguments plus engine trace values.

JSON values, `datetime`, `date`, `time`, `Decimal`, `UUID`, tuples, sets and frozen sets round-trip through saved arguments.
Dictionary keys must be strings. Unsupported objects are refused before saving a job.
Datetime conversion preserves the instant and numeric offset, but not the original zone identity or `fold` flag.
Unknown or malformed saved type tags fail restoration rather than reaching business code as a different type.

The engine manages `tr_id`, `root_id`, `parent_id`, `process_class`, `owning_process_class` and runtime `context`.
`process_class` and `owning_process_class` are dotted-path strings, not Python class objects.
`context` is a shared scratch dictionary for the hooks of one execution. It is not saved, and each retry starts with an empty dictionary.
It differs from `submission_context` (transactional preparation) and `JOB_CONTEXT` (saved application audit context).
`user_id` is written from `user=` and restored by the worker.
`state`, `exception` and `request` are reserved at the call boundary.
A failure callback receives `exception=` after the engine handles the failure:

```python
def report_failure(instance, exception, **kwargs):
    logger.error('Order %s failed', instance.pk, exc_info=exception)
```

Failure callbacks see `failed_state` after a successful failure-state save.
Any action they call must be available from that state.
When maintenance completes a job at its error limit, it reconstructs `RuntimeError('[detect_stuck] <saved error text>')`.
That exception has neither the original exception class nor its traceback. Failure callbacks must not rely on either being available;
log the saved text and use the recorded job identifier to inspect the earlier attempt.

## Settings

Set these keys inside `DJANGO_LOGIC`. Omitted keys use their defaults.

| Key | Default | Meaning |
| --- | --- | --- |
| `BACKGROUND_EXECUTION` | `'pull'` | Workers run saved jobs; `'sync'` runs inline and requires `enable_sync()` before startup. |
| `DEFAULT_QUEUE` | `'django_logic'` | Queue for declarations without `queue=`. |
| `LOCK_TIMEOUT` | `7200` | Positive ownership-lease duration in seconds. Confirmed job-process exits release their locks sooner. An unobserved interruption can leave ownership until expiry; expiry does not prove an external request stopped. |
| `TRANSITION_MESSAGE_MAX_ERRORS` | `5` | Positive integer limit on recorded errors before a job completes as failed. |
| `TRANSITION_MESSAGE_RETRY_MINUTES` | `2` | Nonnegative wait after a recorded error. |
| `TRANSITION_MESSAGE_CLEANUP_DAYS` | `7` | Age before completed job history is eligible for cleanup; the newest failed job per record and process is retained. |
| `JOB_CONTEXT` | `None` | Dotted adapter class that captures application context at submission and restores it for each attempt. |
| `JOB_PROCESS_FINISH` | `None` | Dotted path to an application function that sends buffered job-process reports before exit. |

The retry window is `max(TRANSITION_MESSAGE_RETRY_MINUTES * (TRANSITION_MESSAGE_MAX_ERRORS + 1), 15)` minutes.
With defaults it is 15 minutes. It helps diagnose work without recent progress; it is not a job deadline.
Use `timeout=` to bound one attempt and `Defer(until=..., reason=...)` for expected waiting.

## Checks and diagnostic messages

Run `python manage.py check --tag django_logic` after configuring the application.

| Check ID | Meaning |
| --- | --- |
| `django_logic.pull_mode_needs_postgresql` | Background workers require PostgreSQL. |
| `django_logic.state_lock_database` | State ownership requires the default PostgreSQL database; explicitly enabled single-process tests may use SQLite. |
| `django_logic.state_lock_routing` | Both ownership tables must be migrated and written on the default database. |
| `django_logic.transition_message_routing` | Jobs and their bound models must use the same default database. |
| `django_logic.unread_setting` | A configured setting is not a supported key. |

| Error marker | Meaning |
| --- | --- |
| `[unrestorable]` | The saved model, process, action or application context cannot be restored. Check the recorded error and deployed configuration. |
| `[superseded]` | The record's state changed, so the saved action no longer applies. |
| `[detect_stuck]` | Maintenance completed a job at its error limit; the following text is its saved error, not an original traceback. |
| `[timeout]` | The job exceeded its declared attempt timeout. |
| `[crashed]` | The job process stopped before it recorded a result. Inspect the worker log for its exit or signal. |

See [running workers](workers.md) for commands, monitoring and connection budgets.
