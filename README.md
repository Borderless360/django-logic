![django-logic](https://user-images.githubusercontent.com/6745569/87846635-dabb1500-c903-11ea-9fae-f1960dd2f82d.png)

[![CI](https://github.com/Borderless360/django-logic/actions/workflows/ci.yml/badge.svg)](https://github.com/Borderless360/django-logic/actions/workflows/ci.yml)
[![Coverage Status](https://coveralls.io/repos/github/Borderless360/django-logic/badge.svg?branch=master)](https://coveralls.io/github/Borderless360/django-logic?branch=master)
[![License](https://img.shields.io/pypi/l/django-logic.svg)](https://github.com/Borderless360/django-logic/blob/master/LICENSE)

Django Logic is a workflow library for Django. You declare the states of a
model, the transitions between them, and the code each transition runs. The
declaration lives in one place, away from views, models and forms.

Work that is slow, external or retriable is a background transition.
django-logic saves it as a database row. A worker process claims the row and
runs it.

## Requirements

- Python 3.11 or later.
- Django 5.1 or later. CI tests 5.1, 5.2 and 6.0.
- PostgreSQL for background transitions. The worker claims a row with
  `SELECT FOR UPDATE SKIP LOCKED`, and SQLite has no row locks.
- A cross-process `default` cache. The web processes and the worker processes
  share the state lock through it. django-logic locks through Django's cache
  API and imports no backend, so `django.core.cache.backends.redis.RedisCache`
  is enough. `manage.py check` refuses a per-process cache (locmem,
  dummy) when `DEBUG=False` and a background transition is bound.

## Install

The current release is **4.1.1**. See the [changelog](CHANGELOG.md) for its fixes.
Before upgrading to 4.0, follow the [upgrade and rollback requirements](docs/operation-contract.md#upgrade-and-rollback).


```bash
pip install django-logic
```

Add one entry to `INSTALLED_APPS` and create the table:

```python
INSTALLED_APPS = [
    ...,
    'django_logic',
]
```

That one entry is the whole library: the `TransitionMessage` table and
its migrations, the `dl_worker` and `dl_transitions` commands, and every
system check. There is nothing else to install or configure.

Running no background transitions? The same entry. The PostgreSQL and
shared-cache rules below apply only when a background transition is
bound — `manage.py check` tells you the moment one is — so a
synchronous-only project runs on SQLite with Django's default cache.
On other databases, a synchronous transition takes its cache lock but opens no transaction.
Without a caller transaction, earlier writes survive an interruption, and swallowed database errors may not be detected.
Use PostgreSQL to verify the transaction and concurrent-state guarantees described below.

```bash
python manage.py migrate
```

Point the `default` cache at a backend the web processes and the workers share:

```python
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': os.environ['REDIS_URL'],
    }
}
```

Every `DJANGO_LOGIC` key has a default, so you need no other configuration to
start.

### Upgrading older installations

Databases on 0.17.x or earlier must first run `migrate` using Django Logic 3.2.
The current package retains the squashed migration and its replacement history.

When upgrading from before 3.0, stop old workers before upgraded producers
submit work. Finish the application upgrade before starting the new workers.
See the [earlier upgrade sequence](https://github.com/Borderless360/django-logic/blob/v3.0.1/docs/operation-contract.md#upgrade-and-rollback).

## Declare a process

A process lists the transitions of one state field. A transition names the
states it starts from, the state it ends in, and the functions it runs.

```python
# models.py
from django.db import models


class Order(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('fulfilled', 'Fulfilled'),
        ('fulfilment_failed', 'Fulfilment failed'),
        ('cancelled', 'Cancelled'),
    ]
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default='draft')
```

```python
# process.py
from django_logic import Process, Transition


def has_stock(instance, **kwargs):
    return all(item.product.stock >= item.quantity for item in instance.items.all())


def is_staff_member(instance, user, **kwargs):
    return user.is_staff


def reserve_stock(instance, **kwargs):
    for item in instance.items.all():
        item.product.stock -= item.quantity
        item.product.save()


def send_approval_email(instance, **kwargs):
    ...


class OrderProcess(Process):
    process_name = 'process'
    transitions = [
        Transition(
            action_name='approve',
            sources=['draft'],
            target='approved',
            conditions=[has_stock],
            permissions=[is_staff_member],
            side_effects=[reserve_stock],
            callbacks=[send_approval_email],
        ),
        Transition(
            action_name='cancel',
            sources=['draft', 'approved'],
            target='cancelled',
        ),
    ]
```

Each declaration slot has one job:

- `conditions` — functions that answer True or False. Every one must answer
  True, or the transition is not available.
- `permissions` — functions that answer whether this user may run the
  transition. They receive `user`.
- `side_effects` — the work of the transition. It runs before the object
  reaches the target state. A failure stops the state change, and django-logic
  writes `failed_state` when you declare one.
- `callbacks` — functions that run after the object reaches the target state.
  They are best-effort: django-logic swallows ordinary exceptions they raise.
  Put required work in `side_effects`.
- `failure_callbacks` — functions that run after a side-effect fails. They
  receive `exception=`. Put cleanup and compensation here.

On PostgreSQL, a lock-taking synchronous `Transition` runs callbacks and `next_transition` inside its transaction, before the target state commits.

Other connections see the prior committed state until the outermost transaction commits, including any transaction opened by the caller.
A process interruption during callbacks can therefore discard the state and side-effect writes too.
Use `transaction.on_commit()` for best-effort work that needs committed data; it does not guarantee delivery of required work.
Pull workers run background success callbacks after the job commits; a crash can lose callbacks after completion.
Inline background execution can still be inside a caller's outer transaction.

Use `DatabaseTransition` for changes that PostgreSQL can roll back completely.
It takes the same declaration arguments as `Transition` and requires PostgreSQL.
It keeps row locks, permissions, conditions, source checks, pending-job checks,
targets, failure handling, callbacks and `next_transition`.
It refuses reentry without creating a persistent cache lock. A killed process
therefore leaves no cache lock that delays a saved completion job's retry.

This database-only promise includes **all nested transitions and submissions**,
including their ordinary callbacks. They must use the same database.
The scope ends before `transaction.on_commit()` callbacks run. Save required
external work as a background job; do not call providers, publish messages,
or perform other irreversible work in this scope. In a synchronous test runtime,
background work runs inline and must also satisfy that promise.

Ordinary `Transition` calls outside this scope keep their shared cache protection.
`DatabaseTransition` also refuses an existing cache lock, including one held by
an external call whose database connection failed. Do not clear that lock to
force a retry. Replacing an external operation with `DatabaseTransition` is unsafe.

GV uses this declaration for import results and their nested stock and bulk updates.
Its order notifications and shipping submission run in separately saved background work.
Declare a `Transition` with no `target` for work that needs conditions,
permissions and side-effects but writes no state on success. It follows
the same rules as every transition: it takes the state lock, it is
refused while a background transition is uncompleted, and it runs
`next_transition`.

Add `lock=False` when the unit of work is not the bound row. A store
process that posts one parcel's tracking is the example: the row is the
store, the work is the parcel, and two parcels must not wait for each
other. A `lock=False` transition takes no state lock and is not refused
while a background transition is uncompleted. It keeps `conditions`,
`permissions`, `side_effects`, `callbacks`, `failure_callbacks` and
`next_transition` — a follow-up is best-effort, so one that takes the lock
is skipped while the row is locked. It cannot declare a `target` or a
`failed_state` — a state write must serialise on that state — and
`BackgroundTransition` refuses it. `get_available_actions()` lists it
while the row is locked, because it can be called.

```python
def post_tracking(instance, **kwargs):
    ...  # tell the store about the parcel in kwargs['parcel']


Transition(
    action_name='post_tracking',
    sources=['approved', 'fulfilled'],
    lock=False,
    side_effects=[post_tracking],
)
```

`permissions` treat a call with no user as permitted. When one action name
is split across nested processes by who is asking, give the person's
branch `permissions_class = StrictPermissions` (refuses a call with no
user) and the automation branch `permissions_class = NoUserPermissions`
(refuses a call with one), both from `django_logic.commands`, so no
caller matches two branches.

## Bind the model to the process

Bind in your app's `AppConfig.ready()`. This is the one supported place.

```python
# apps.py
from django.apps import AppConfig
from django_logic import ProcessManager


class ShopConfig(AppConfig):
    name = 'shop'

    def ready(self):
        from .models import Order
        from .process import OrderProcess
        ProcessManager.bind_model_process(Order, OrderProcess, state_field='status')
```

Import the model and the process **inside** `ready()`. A process references its
model, and so do its condition, permission and side-effect functions. Binding
at module import time therefore builds the import cycle
`models.py → process.py → actions.py → models.py`. Django loads every app's
models before it runs any `ready()`, so binding here cannot build that cycle,
and your action modules import the model at the top level like normal code.

List the app in `INSTALLED_APPS`, or Django never runs `ready()`.

One physical model and process name must identify one state field, including
bindings through proxy models. Give independent fields distinct process names.
Binding sibling proxies with the same process name to different fields is
unsupported. The library does not validate that combination.

For an inherited state field on a multi-table model, drive the process through
the parent model that owns the field. Mixing parent and child process access
is unsupported. Their background identities and pending-work probes do not
share one gate. Use a separate concrete model for an independent workflow.

## Run a transition

```python
order = Order.objects.get(pk=pk)
order.process.approve(user=request.user)
```

- The accessor is the process class's `process_name`. It is `process` by
  default.
- Pass `user=` in a request handler. A call without `user=` is a system call,
  and it skips every permission check.
- Never pass the request. A transition refuses `request=` at the call:
  hooks are `fn(instance, **kwargs)` and run on a worker for a background
  transition, where no request exists. Resolve what a hook needs in the
  view and pass plain values.
- `order.process.get_available_actions(user=request.user)` lists what this user
  may run right now.
- A refused transition raises `TransitionNotAllowed` from
  `django_logic.exceptions`. Its subclass `TransitionTemporarilyUnavailable`
  means the instance is busy, so the caller may retry shortly. Catch the
  subclass first.

The exception's `reason` is a `RefusalReason` from `django_logic.exceptions`.
Its values are strings and can be included in JSON responses. Process calls
also attach `user_message`, ready to display. Existing exception classes,
diagnostic text from `str(error)`, and exception arguments stay unchanged.

| Reason value | What refused the call |
| --- | --- |
| `permission` | A process or transition permission check failed. |
| `condition` | A process or transition condition check failed. |
| `source_state` | The current or persisted state is outside the transition's sources. |
| `unknown_action` | No process declaration has this action name. |
| `locked` | The state lock could not be acquired. |
| `background_in_flight` | An uncompleted background transition still has retry coverage. |
| `background_stranded` | An uncompleted background transition is no longer being retried. |
| `ambiguous` | More than one transition passed the checks for this action. |

Reasons describe the existing checks; they do not change retry behavior.
A PostgreSQL row-lock timeout raises `TransitionTemporarilyUnavailable` with reason `locked`.
A cache state-lock refusal raises plain `TransitionNotAllowed` with that same reason.
Use the exception type, not `reason='locked'` alone, to decide whether to suggest a short retry.
When declarations share an action name, prefer a transition whose sources
include the current state and whose process permissions and conditions pass.
Its failed check supplies the reason. Otherwise, use a process guard that
blocks a declaration for the current state. If all declarations require
another source state, report `source_state`.

Declaration order resolves ties within each group. A custom check that
supplies no reason keeps `None`, even if a lower-priority refusal supplies
a reason. This rule does not infer a unique intended branch or list every
failed check. Process guards still run before their transitions and nested
processes.

The resolver captures reasons during the existing checks. It does not run
permissions or conditions again to explain a refusal.
Call `get_available_actions()` explicitly when the caller needs a fresh action list.

A custom `is_valid` override can return `False` without explaining why.
Its reason is `None` unless a stock check on that same object recorded a
failure. Caller-created exceptions also default to `None`. Continue to catch
`TransitionNotAllowed` when handling these refusals.

### Refusal messages

Process calls provide generic messages, such as
`Action 'submit' is not allowed: this record does not meet the requirements for this action.`
Pending background work uses `is temporarily unavailable` in the prefix.
An unknown reason code or a custom check with no reason uses
`Action 'submit' is not allowed`.

Use `Process.refusal_messages` for process-wide wording and the same option
on a transition for its wording. These mappings contain overrides only.
Use `@refusal_message(text)` for one condition or permission callable:

```python
from django_logic import Process, Transition
from django_logic.commands import refusal_message
from django_logic.exceptions import RefusalReason, TransitionNotAllowed


@refusal_message("Upload an invoice before submitting.")
def has_invoice(claim):
    return claim.invoices.exists()


class ClaimProcess(Process):
    refusal_messages = {
        RefusalReason.PERMISSION: "Only the claim owner can perform this action.",
    }
    transitions = [
        Transition(
            "submit", sources=["draft"], target="submitted",
            conditions=[has_invoice],
            refusal_messages={
                RefusalReason.SOURCE_STATE: "This claim has already been submitted.",
            },
        ),
    ]


try:
    claim.process.submit(user=request.user)
except TransitionNotAllowed as error:
    response = {"reason": error.reason, "message": error.user_message}
```

Values are literal explanation text. The library adds the action prefix;
it does not substitute templates or call message functions. The decorator
returns the same callable with the same signature and behavior.

The first failed callable's message takes precedence over the transition
map, then the selected nested process map, the root process map, and built-in
defaults. A nested process with no override keeps the root's wording.
A process-level failure uses process messages because no transition was
selected. Only checks already evaluated can supply a message.

Message capture does not repeat checks or change short-circuit behavior.
Nested refusals keep an already supplied `user_message`. Technical exceptions
retain their normal behavior. Direct `Transition.change_state()` calls can
leave `user_message=None`; process configuration applies to Process calls.

## Background transitions

`BackgroundTransition` runs its side-effects on a worker process, and retries
them. Declared with no `target`, it changes no state on success — same
durability, same rules. Import it from `django_logic.background`.

```python
# process.py
from django_logic import Process, Transition
from django_logic.background import BackgroundTransition


class OrderProcess(Process):
    transitions = [
        Transition(action_name='approve', sources=['draft'], target='approved'),
        BackgroundTransition(
            action_name='fulfil',
            sources=['approved'],
            target='fulfilled',
            in_progress_state='fulfilling',
            failed_state='fulfilment_failed',
            queue='django_logic.critical',
            timeout=600,
            side_effects=[book_courier, print_labels],
            callbacks=[send_tracking_email],
        ),
    ]
```

```python
# views.py — returns as soon as django-logic saves the row.
order.process.fulfil(user=request.user)
```

The call writes `in_progress_state` and one `TransitionMessage` row in a single
transaction, then returns. By default, a worker runs the side-effects and
writes the target state in one transaction. A failed attempt rolls back
its own database writes and becomes claimable again after
`TRANSITION_MESSAGE_RETRY_MINUTES`. After `TRANSITION_MESSAGE_MAX_ERRORS`
attempts django-logic writes `failed_state`, runs `failure_callbacks` and
completes the row.

`in_progress_state` is available only on background transitions with a target.
Several transitions may share it; each saved job records its exact transition.
A synchronous transition cannot declare it. For synchronous preparation, first
save a real busy state, then start a background transition with `next_transition`.
Provide a periodic task to restart records left in that busy state if the
caller stops before saving the background job.

If another action changes the model's state while a job waits, that change wins.
The worker completes the job as `superseded` and skips its side effects.

- **Side-effects must be idempotent against external systems.** A retry runs
  them again from the start, so a payment or an email can happen twice.
- `atomic=False` preserves committed model writes when later work fails. Use it
  for provider receipts that must survive a report or state-write failure.
  See [provider receipts](docs/recipes/provider-receipts.md) for restrictions and
  retry safety. The default `atomic=True` behavior is unchanged.
- `queue=` is optional. A transition without it runs on
  `DJANGO_LOGIC['DEFAULT_QUEUE']`, which is `django_logic`. Name a queue per
  service level and give it its own worker.
- `timeout=` is optional. The worker kills an attempt that runs past it and
  records one error on the row.
- Raise `PermanentFailure` from `django_logic.background` when a retry cannot
  help. The worker then takes the failure path on the first attempt. For an
  exception type you do not own, list it in `no_retry_on=(CustomsRefusal,)`.
- While a row is uncompleted, a second background transition on the same
  instance and process raises `AlreadyInProgress`, and a synchronous transition
  on it raises `TransitionTemporarilyUnavailable`. Start follow-up work from a
  callback, which runs after django-logic completes the row.

[docs/design/PULL_WORKERS.md](docs/design/PULL_WORKERS.md) explains how a
worker claims a row and what happens when one dies.

## Run the workers

One `dl_worker` process serves one group of queues.

If logging or telemetry buffers reports, configure
`DJANGO_LOGIC['JOB_PROCESS_FINISH']` with an application function's dotted path.
The function sends this job process's reports before exit, with a separate
two-second limit. See [buffered reporting](docs/logger.md#send-buffered-reports-before-a-job-process-exits)
for process-local state requirements and delivery limits.

```bash
python manage.py dl_worker --queues django_logic.critical,django_logic.fast
python manage.py dl_worker --queues django_logic.slow --concurrency=4
```

`--concurrency=N` says how many attempts one worker runs at a time. The default
is 1. Each attempt runs in its own forked process and holds its own database
connection. Read "Sizing a deployment" in
[docs/design/PULL_WORKERS.md](docs/design/PULL_WORKERS.md) for the memory and
connection budget.

A worker that dies releases its row lock with its database connection, and the
next claim takes the row.

### See what is not moving

```bash
python manage.py dl_transitions
python manage.py dl_transitions --send 1234
python manage.py dl_transitions --stats --since 7d --queues django_logic --json
```

`dl_transitions` lists every uncompleted background transition and says why
each one is not moving: it has spent every attempt, it waits out the retry
pause, its last attempt has no recorded result, or it waits for worker capacity.
The report also distinguishes recent worker presence from actual queue progress. `--queues` narrows the list.
Workers take turns between eligible clients. Within a client, jobs run by when they became ready, then submission time and ID.
A postponed older job can therefore follow newer work that was ready first.

Workers record their served queues during each maintenance check, even when all
job slots are full. This recent observation is separate from queue progress.
A queue with no progress still reports an error when its worker is present.
The message directs operators to check capacity and blocked jobs.

`--stats` reports retained jobs created during the selected period, grouped by
queue, model, process, transition, parent action and effect. It includes outcomes, errors,
recorded starts, repeated starts and deferrals. Initial waiting uses the first
recorded start. Duration covers the last measured attempt, excluding completion
callbacks. Each timing includes its sample count, median, 90th percentile and
maximum in milliseconds. Missing history stays unknown. Repeated starts include
deferrals and competing starts; they are not an exact retry count. The report
cannot infer total runtime or worker utilization. It changes no job records.
Use `--json` for machine-readable output. `--queue` also accepts the queue filter.

The same report is available as
`django_logic.background.transition_statistics(since=cutoff, queues=None, using=None)`.
`cutoff` is a datetime; the optional database alias selects where to read jobs.
`queue_worker_presence(queue_name)` returns `True` for recent presence, `False`
for no retained observation, or `None` when the cache cannot answer.

`--send <pk>` clears the retry wait on one row and wakes the workers, so the
next claim takes it. The command runs no side-effects itself.

### Two safety nets

The worker starts two safety nets once a minute in one separate process.
Only one safety-net process runs per worker. Its time limit is 60 seconds.
Nothing else needs a schedule.

- `detect_stuck_transitions` completes a job that has spent every allowed error.
  It writes `failed_state` and runs failure callbacks only when the saved state still applies and can be checked under its lock.
  Otherwise it records why the state change was skipped. It also reports rows that waited past the retry window with no attempt, in one
  line per queue. If a worker started a row from that queue inside the window,
  or holds one now, the queue is behind, and the line is a WARNING. If not, no
  worker has taken a row from the queue for the whole window, and the line is
  an ERROR. Either no worker serves the queue, or work from other queues fills
  every slot of the workers that do. Recent worker presence changes the
  explanation, but does not reduce the severity of missing progress.
- `cleanup_completed_transitions` deletes completed rows older than
  `TRANSITION_MESSAGE_CLEANUP_DAYS`. It keeps the newest failed row per
  instance and process, because that row is the only explanation for an
  instance parked in its `failed_state`.

Alert when the worker processes stop. The safety nets stop with them.
Reserve one extra database connection for the safety-net process, plus any
connections your callbacks open. A slow or exiting callback cannot block
the supervisor's attempt timeouts. If a pass stops, the next pass checks
the remaining uncompleted rows. Callbacks for completed rows remain
best-effort and are not retried.

## Test your process

`django_logic.testing` gives you `ProcessScenario`, a test base class that runs
a whole workflow inline, background transitions included. A test reads like the
business process.

```python
from django_logic.testing import ProcessScenario


class TestOrderFulfilment(ProcessScenario):
    process_class = OrderProcess
    model = Order
    state_field = 'status'

    def test_happy_path(self):
        order = self.create_instance(status='approved')
        self.background_transition(order, 'fulfil')
        self.assert_state(order, 'fulfilled')
        self.assert_side_effects_ran(['book_courier'])
```

For deployment-wide inline tests, call `django_logic.conf.enable_sync()` in test settings and set
`DJANGO_LOGIC['BACKGROUND_EXECUTION'] = 'sync'`. Boot refuses that setting without the explicit opt-in.
Production deployments normally use `pull`. Business code can use `django_logic.background.sync_execution()` for one block that needs an immediate result.
That block runs in the caller's process and starts no retry loop.
A committed unfinished job can still be claimed by workers serving its queue.
An atomic background transition can join a caller transaction. `atomic=False` still requires PostgreSQL autocommit.
[docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) has the test setup and scenario catalog.

## More documentation

- [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) — how to test a process, and
  the assertions `ProcessScenario` gives you.
- [docs/design/PULL_WORKERS.md](docs/design/PULL_WORKERS.md) — how the workers
  claim and run rows, how to size a deployment, and how to run behind
  pgbouncer.
- [docs/recipes/nested-processes.md](docs/recipes/nested-processes.md) — how a
  parent drives many children without cascading their failures.
- [docs/recipes/long-jobs.md](docs/recipes/long-jobs.md) — how to split work
  that runs for a long time.
- [docs/logger.md](docs/logger.md) — the loggers and the events they write.
- [CHANGELOG.md](CHANGELOG.md) — what each release changed, and every upgrade
  step.

## Contributing

Pull requests are welcome. Open an issue first for a major change.

```bash
pip install -e '.[dev]'
python tests/manage.py test          # SQLite suite
python tests/manage.py test tests.test_transition
```

Add a test for every change, and update the documentation the change touches.

Report a bug in the
[issue tracker](https://github.com/Borderless360/django-logic/issues).

## License

[MIT](https://choosealicense.com/licenses/mit/)
