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
- Django 4.2 or later. CI tests 4.2, 5.1, 5.2 and 6.0. Django 5.0 is not supported.
- PostgreSQL for background transitions. The worker claims a row with
  `SELECT FOR UPDATE SKIP LOCKED`, and SQLite has no row locks.
- A cross-process `default` cache. The web processes and the worker processes
  share the state lock through it. django-logic locks through Django's cache
  API and imports no backend, so `django.core.cache.backends.redis.RedisCache`
  is enough. Boot refuses a per-process cache (locmem, dummy) when
  `DEBUG=False`.

## Install

```bash
pip install django-logic
```

Add `[redis]` if your settings name `django_redis.cache.RedisCache`. That
extra installs the third-party backend for you.

Add two entries to `INSTALLED_APPS` and create the table:

```python
INSTALLED_APPS = [
    ...,
    'django_logic',
    'django_logic.background',
]
```

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
  They are best-effort: django-logic swallows what they raise.
- `failure_callbacks` — functions that run after a side-effect fails. They
  receive `exception=`. Put cleanup and compensation here.

Use `Action` instead of `Transition` for work that needs conditions,
permissions and side-effects but changes no state.

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

## Run a transition

```python
order = Order.objects.get(pk=pk)
order.process.approve(user=request.user)
```

- The accessor is the process class's `process_name`. It is `process` by
  default.
- Pass `user=` in a request handler. A call without `user=` is a system call,
  and it skips every permission check.
- `order.process.get_available_actions(user=request.user)` lists what this user
  may run right now.
- A refused transition raises `TransitionNotAllowed` from
  `django_logic.exceptions`. Its subclass `TransitionTemporarilyUnavailable`
  means the instance is busy, so the caller may retry shortly. Catch the
  subclass first.

## Background transitions

`BackgroundTransition` runs its side-effects on a worker process, and retries
them. `BackgroundAction` does the same and changes no state on success. Import
both from `django_logic.background`.

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
transaction, then returns. A worker claims that row, runs the side-effects and
writes the target state, all in one atomic block. A failed attempt rolls back
its own database writes and becomes claimable again after
`TRANSITION_MESSAGE_RETRY_MINUTES`. After `TRANSITION_MESSAGE_MAX_ERRORS`
attempts django-logic writes `failed_state`, runs `failure_callbacks` and
completes the row.

- **Side-effects must be idempotent against external systems.** A retry runs
  them again from the start, so a payment or an email can happen twice.
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
```

`dl_transitions` lists every uncompleted background transition and says why
each one is not moving: it has spent every attempt, it waits out the retry
pause, a worker runs it now, or no worker serves its queue. `--queues` narrows
the list.

`--send <pk>` clears the retry wait on one row and wakes the workers, so the
next claim takes it. The command runs no side-effects itself.

### Two safety nets

The worker loop runs two safety nets once a minute. Nothing else needs a
schedule.

- `detect_stuck_transitions` finalizes a row that has spent every attempt: it
  writes `failed_state`, runs `failure_callbacks` and completes the row. It
  also reports a row that waited past the retry window with no attempt, which
  means no worker serves that row's queue.
- `cleanup_completed_transitions` deletes completed rows older than
  `TRANSITION_MESSAGE_CLEANUP_DAYS`. It keeps the newest failed row per
  instance and process, because that row is the only explanation for an
  instance parked in its `failed_state`.

Alert when the worker processes stop. The safety nets stop with them.

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

Sync execution runs the worker path inline, for tests only. A test settings
module calls `django_logic.conf.enable_sync()` and sets
`DJANGO_LOGIC['BACKGROUND_EXECUTION'] = 'sync'`. Boot refuses that value
anywhere else. [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) has the setup and
the full scenario catalog.

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

make build                           # or run the same suite in Docker
make test
make test-one t=tests.test_transition
```

Add a test for every change, and update the documentation the change touches.

Report a bug in the
[issue tracker](https://github.com/Borderless360/django-logic/issues).

## License

[MIT](https://choosealicense.com/licenses/mit/)
