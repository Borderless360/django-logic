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
- Django 5.2 or later. CI tests 5.2, 6.0 and 6.1.
- PostgreSQL for background transitions. The worker claims a row with
  `SELECT FOR UPDATE SKIP LOCKED`, and SQLite has no row locks.
  Use psycopg 3.2 or later, or Django’s supported psycopg2 driver.
  The `postgres` installation extra installs psycopg 3.
- The `default` PostgreSQL database stores committed state-lock ownership for every
  transition caller. SQLite is supported for single-process business tests with the
  test runtime enabled. Other database backends are refused.
- A shared cache is useful for worker-presence observations. State locks do not use it.

## Install

The current release is **5.2.1**. See the [changelog](https://github.com/Borderless360/django-logic/blob/master/CHANGELOG.md) for its fixes.
Before upgrading, follow the [upgrade and rollback requirements](https://github.com/Borderless360/django-logic/blob/master/docs/upgrading.md).


```bash
pip install "django-logic[postgres]"
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
system check. Configure the database and cache described below.

Synchronous-only applications use the same entry and ownership tables. PostgreSQL
keeps ownership committed separately from business changes. SQLite tests use their
ordinary connection and cannot establish concurrent production guarantees.

```bash
python manage.py migrate
```

For worker-presence observations across processes, install the cache backend you use
and point `default` at a shared service. For example, with the `redis` package:

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

### Upgrading to version 5

Version 5 removes obsolete APIs and saved-job restoration fallbacks. Start from
4.3.2, update application declarations, and finish jobs whose action or process
names will change before stopping the old producers and workers. Apply migrations
with the new application installed, then start only the new version’s processes. Migration 0017
refuses unresolved jobs that the new code cannot restore; it does not discard them.
Mixed versions are unsupported. See the [upgrade checklist](https://github.com/Borderless360/django-logic/blob/master/docs/upgrading.md).

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
        ('fulfilling', 'Fulfilling'),
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

Each declaration option has one purpose:

- `conditions` — functions that answer True or False. Every one must answer
  True, or the transition is not available.
- `permissions` — functions that answer whether this user may run the
  transition. They receive `user`.
- `side_effects` — the work of the transition. It runs before the object
  reaches the target state. A failure stops the state change, and django-logic
  writes `failed_state` when you declare one.
- `callbacks` — functions that run after the object reaches the target state.
  They are best-effort: django-logic swallows ordinary exceptions they raise.
  A callback that fails keeps what it saved before the error. Only a database
  error discards its writes. A transition that a callback starts keeps its
  failed state when it fails.
  Put required work in `side_effects`.
- `failure_callbacks` — functions that run after a side-effect fails. They
  receive `exception=` and are best-effort. Use them for optional reporting.
  Required compensation belongs in a saved business job.

Required database changes can use `atomic=True`. Use `DatabaseTransition` when all work can be rolled back by PostgreSQL.
See [locks and transactions](https://github.com/Borderless360/django-logic/blob/master/docs/transactions.md) for callback timing, nested actions and external-request limits.

Declare a `Transition` with no `target` for work that needs conditions,
permissions and side-effects but writes no state on success. It follows
the same rules as every transition: it takes the state lock, it is
refused while a background transition is uncompleted, and it runs callbacks after success.

An explicit `target=''` saves an empty state. Use `target=None` to leave the state unchanged.
An empty target keeps the usual source checks and state locks, including for background transitions.

Add `lock=False` when the unit of work is not the bound row. A store
process that posts one parcel's tracking is the example: the row is the
store, the work is the parcel, and two parcels must not wait for each
other. A `lock=False` transition takes no state lock and is not refused
while a background transition is uncompleted. It keeps `conditions`,
`permissions`, `side_effects`, `callbacks` and `failure_callbacks`.
A callback is best-effort; a transition it starts can be refused while the row is locked. A `lock=False` transition cannot declare a `target` or a
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
share one unfinished-job check. Use a separate concrete model for an independent workflow.

## Run a transition

```python
order = Order.objects.get(pk=pk)
order.process.approve(user=request.user)
```

Use business argument names that do not conflict with the engine's trace identifiers.
See the [saved arguments reference](https://github.com/Borderless360/django-logic/blob/master/docs/reference.md#saved-arguments)
for reserved names and the temporary `context` dictionary shared by hooks.

- The accessor is the process class's `process_name`. It is `process` by
  default.
- Pass `user=` in a request handler. A call without `user=` is a system call,
  and it skips every permission check.
- Never pass the request. A transition refuses `request=` at the call:
  side effects and callbacks accept `fn(instance, **kwargs)` and can run on a worker.
  Conditions receive `instance`; permissions receive `instance, user`. Neither receives business arguments. Resolve what a hook needs in the
  view and pass plain values.
- `order.process.get_available_actions(user=request.user)` lists what this user
  may run right now.
- A refused transition raises `TransitionNotAllowed` from
  `django_logic.exceptions`. Its subclass `TransitionTemporarilyUnavailable`
  means the instance is busy, so the caller may retry shortly. Catch the
  subclass first.

See [refused actions](https://github.com/Borderless360/django-logic/blob/master/docs/refusals.md) for reason codes, temporary refusals and messages for users.

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
A synchronous transition cannot declare it. Use `submission_context` for required
database preparation that must commit together with the background job and its
in-progress state. A failed submission rolls all of them back. See the
[submission contract](https://github.com/Borderless360/django-logic/blob/master/docs/background-jobs.md#preparing-a-submission).

If another action changes the model's state while a job waits, that change wins.
The worker completes the job as `superseded` and skips its side effects.

- **Side-effects must be idempotent against external systems.** A retry runs
  them again from the start, so a payment or an email can happen twice.
- `atomic=False` preserves committed model writes when later work fails. Use it
  for provider receipts that must survive a report or state-write failure.
  See [provider receipts](https://github.com/Borderless360/django-logic/blob/master/docs/recipes/provider-receipts.md) for restrictions and
  retry safety.
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
  on it raises `TransitionTemporarilyUnavailable`. Keep required functions in
  the job that performs the work, or save business jobs with the result that requests them.
  Callbacks run after completion and are suitable only for optional work.

## Run the workers

One `dl_worker` process serves one group of queues.

```bash
python manage.py dl_worker --queues django_logic.critical,django_logic.fast
python manage.py dl_worker --queues django_logic.slow --concurrency=4
```

If logging or telemetry buffers reports, configure
`DJANGO_LOGIC['JOB_PROCESS_FINISH']` with an application function's dotted path.
The function sends this job process's reports before exit, with a separate
two-second limit. See [buffered reporting](https://github.com/Borderless360/django-logic/blob/master/docs/logger.md#send-buffered-reports-before-a-job-process-exits)
for process-local state requirements and delivery limits.

`--concurrency=N` says how many attempts one worker runs at a time. The default
is 1. Each attempt runs in its own job process and holds a database connection.
See [running workers](https://github.com/Borderless360/django-logic/blob/master/docs/workers.md) for deployment size, queue diagnostics, retry commands and maintenance.

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
See the [testing guide](https://github.com/Borderless360/django-logic/blob/master/docs/TESTING_GUIDE.md) for database requirements, transaction behavior and the scenario catalog.

## More documentation

- [API and settings reference](https://github.com/Borderless360/django-logic/blob/master/docs/reference.md) — declarations, imports and configuration.
- [Background jobs](https://github.com/Borderless360/django-logic/blob/master/docs/background-jobs.md) — accepted inputs, required work, fairness and deferral.
- [Locks and transactions](https://github.com/Borderless360/django-logic/blob/master/docs/transactions.md) — database changes, callbacks and external requests.
- [Refused actions](https://github.com/Borderless360/django-logic/blob/master/docs/refusals.md) — reason codes and messages for users.
- [Upgrading](https://github.com/Borderless360/django-logic/blob/master/docs/upgrading.md) — supported migration and rollback requirements.

- [Testing guide](https://github.com/Borderless360/django-logic/blob/master/docs/TESTING_GUIDE.md) — how to test a process, and
  the assertions `ProcessScenario` gives you.
- [Running workers](https://github.com/Borderless360/django-logic/blob/master/docs/workers.md) — how the workers
  claim and run rows, how to size a deployment, and how to run behind
  pgbouncer.
- [Waiting for work on related records](https://github.com/Borderless360/django-logic/blob/master/docs/recipes/related-records.md) — how an order waits for several fulfilments and records their results.
- [Long jobs](https://github.com/Borderless360/django-logic/blob/master/docs/recipes/long-jobs.md) — how to split work
  that runs for a long time.
- [Interactive and automatic requests](https://github.com/Borderless360/django-logic/blob/master/docs/recipes/interactive-and-automatic.md) — shared business functions with separate worker queues.
- [Logging](https://github.com/Borderless360/django-logic/blob/master/docs/logger.md) — the loggers and the events they write.
- [Changelog](https://github.com/Borderless360/django-logic/blob/master/CHANGELOG.md) — what each release changed, and every upgrade
  step.

## Contributing

Pull requests are welcome. Open an issue first for a major change.

```bash
pip install -e '.[dev]'
python tests/manage.py test          # SQLite suite
python tests/manage.py test tests.test_transition
```

Test changed behavior and update the affected documentation.

Report a bug in the
[issue tracker](https://github.com/Borderless360/django-logic/issues).

## License

[MIT](https://choosealicense.com/licenses/mit/)
