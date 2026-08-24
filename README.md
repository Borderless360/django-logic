![django-logic](https://user-images.githubusercontent.com/6745569/87846635-dabb1500-c903-11ea-9fae-f1960dd2f82d.png)

[![CI](https://github.com/Borderless360/django-logic/actions/workflows/ci.yml/badge.svg)](https://github.com/Borderless360/django-logic/actions/workflows/ci.yml)
[![Coverage Status](https://coveralls.io/repos/github/Borderless360/django-logic/badge.svg?branch=master)](https://coveralls.io/github/Borderless360/django-logic?branch=master)
[![License](https://img.shields.io/pypi/l/django-logic.svg)](https://github.com/Borderless360/django-logic/blob/master/LICENSE)
     
Django Logic is a workflow library for Django. You declare transitions, permissions, and side-effects in one place, away from views, models, and forms. Work that must retry or run later is a background transition: a worker claims the row from the database.

## Table of Contents
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Core Concepts](#core-concepts)
- [Usage](#usage)
- [Complete Example](#complete-example)
- [Background Transitions](#background-transitions)
- [Testing Your Processes](#testing-your-processes)
- [Contributing](#contributing)
- [License](#license)

## Features
- 🎯 **Clear Business Logic** - Separate business logic from views, models, and forms
- 🔒 **Built-in Permissions** - Define who can perform which transitions
- 🔄 **Side Effects** - Execute functions during state transitions
- 🏗️ **Nested Processes** - Build complex workflows with sub-processes
- ⚡ **Built-in Locking** - Cache/Redis-based locking to prevent race conditions
- ⏳ **Durable Background Transitions** - Workers claim committed rows straight from the database — no broker, nothing to lose or duplicate. Queue-routed, retried, and recovered after a crash (see [Background Transitions](#background-transitions))
- 🧪 **Scenario-Based Testing** - Test a whole workflow as ordinary unit tests, background jobs, failures and retries included. Sync execution mode and `django_logic.testing` need no services at all (see [Testing Your Processes](#testing-your-processes))
- 🔍 **Structured Logging** - State changes go to the standard `django-logic` / `django-logic.transition` Python loggers. Configure them in Django `LOGGING` (see [docs/logger.md](docs/logger.md))

## Requirements
- Python 3.11+
- Django 4.2+ (4.2, 5.1, 5.2 and 6.0 are tested in CI; 5.0 is not supported)
- django-model-utils >= 4.5.1
- PostgreSQL for background transitions — the worker's claim needs `SELECT FOR UPDATE SKIP LOCKED`; sync mode runs anywhere
- A **cross-process `default` cache** for the state lock — *not* a package dependency. The engine locks through Django's cache API, so any cross-process backend works. That includes `django.core.cache.backends.redis.RedisCache`, built into Django since 4.0. Pull mode refuses to boot on a locmem/dummy cache when `DEBUG=False`.

Extras:
- `pip install django-logic[redis]` — installs `django-redis`, for deployments whose settings name `django_redis.cache.RedisCache`. It stopped being a core dependency in 0.11.0, because the engine has never imported it.
- `[celery]` remains an **empty alias**, so existing `pip install django-logic[celery,redis]` pins keep resolving — 0.16.0 removed the broker, so nothing imports celery

## Installation

```bash
# Installs the current release from PyPI.
# Add [redis] if your settings name django_redis.cache.RedisCache.
pip install django-logic
```

This README documents the current release line (the API introduced in 0.4).
The API changed a lot in 0.2–0.4. If you upgrade from a release before 0.2 — the
old DRF/Celery-coupled API — read [CHANGELOG.md](CHANGELOG.md) first. It lists
every breaking change and the steps to migrate.

## Quick Start

Here's a simple example to get you started:

```python
# models.py
from django.db import models

class Order(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    ]
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')
    # ... other fields

# process.py
from django_logic import Process, Transition

class OrderProcess(Process):
    transitions = [
        Transition(
            action_name='pay',
            sources=['pending'],
            target='paid'
        ),
        Transition(
            action_name='ship',
            sources=['paid'],
            target='shipped'
        ),
        Transition(
            action_name='deliver',
            sources=['shipped'],
            target='delivered'
        ),
        Transition(
            action_name='cancel',
            sources=['pending', 'paid'],
            target='cancelled'
        ),
    ]

# apps.py — bind the process in your app's AppConfig.ready(). This is the only
# supported place to bind. ready() runs after every app's models are loaded, so
# binding here cannot create the model -> process -> actions -> model circular
# import that binding in models.py or process.py creates. See "Bind the
# process" below.
from django.apps import AppConfig
from django_logic import ProcessManager

class OrdersConfig(AppConfig):
    name = 'orders'

    def ready(self):
        from .models import Order
        from .process import OrderProcess
        ProcessManager.bind_model_process(Order, OrderProcess, state_field='status')

# Usage
order = Order.objects.create()
order.process.pay()  # Changes status from 'pending' to 'paid'
```

## Core Concepts

### Definitions
- **Transition** - Changes the state of an object from one state to another. It holds conditions, permissions, side-effects, callbacks and failure callbacks.
- **Action** - Like a transition, but it does not change the state. Use it for work that needs permissions and side-effects only.
- **Side-effects** - Functions that run during a transition, before the object reaches the target state. If one of them fails the state does not advance, and django-logic writes `failed_state` when you declare one. A failed background attempt also rolls back the database writes it made (a savepoint). A synchronous side-effect's writes stay.
- **Callbacks** - Functions that run after the object reaches the target state.
- **Failure callbacks** - Functions that run after a side-effect fails. django-logic writes `failed_state` and unlocks the state first. Put cleanup and compensation here.
- **Conditions** - Functions that must return True for a transition to be allowed.
- **Permissions** - Functions that check if a user can perform a transition.
- **Process** - Groups related transitions with common conditions and permissions.

## Usage
### 1. Add to INSTALLED_APPS
```python
INSTALLED_APPS = (
    ...
    'django_logic',
    ...
)
```

### 2. Define django model with one or more state fields
```python
from django.db import models


MY_STATE_CHOICES = (
     ('draft', 'Draft'),
     ('approved', 'Approved'),
     ('paid', 'Paid'),
     ('void', 'Void'),
 )

class Invoice(models.Model):
    my_state = models.CharField(choices=MY_STATE_CHOICES, default='draft', max_length=16, blank=True)    
    my_status = models.CharField(choices=MY_STATE_CHOICES, default='draft', max_length=16, blank=True)
    is_available = models.BooleanField(default=True)
    
```

### 3. Define a process class with some transitions
```python
from django_logic import Process as BaseProcess, Transition, Action
from .models import MY_STATE_CHOICES


# Define your side effect functions
def update_data(instance, **kwargs):
    # Update instance data
    for key, value in kwargs.items():
        if hasattr(instance, key):
            setattr(instance, key, value)
    instance.save()

class MyProcess(BaseProcess):
    transitions = [
        Transition(action_name='approve', sources=['draft'], target='approved'),
        Transition(action_name='pay', sources=['approved'], target='paid'),
        Transition(action_name='void', sources=['draft', 'approved'], target='void'),
        # An Action runs side-effects without changing state. `sources` lists
        # the states it's available from (required — there is no wildcard).
        Action(action_name='update', sources=['draft', 'approved'], side_effects=[update_data]),
    ]
```

### 4. Bind the process in your app's `AppConfig.ready()`

**Binding happens in exactly one place: your app's `AppConfig.ready()`.** Do
**not** bind at module import time (in `models.py` or `process.py`).

A process references its model, and so do its side-effect, condition and
permission functions. Binding `Model ⇄ Process` at import time therefore forces
`models.py → process.py → actions.py → models.py` — a circular import. The only
way out is a `from .models import X` line inside every action function.
`ready()` removes the cycle instead. Django imports **all** apps' models before
it runs **any** `ready()`, so every model already exists when you bind, and your
action modules can import the model at the top level like normal code.

```python
# apps.py
from django.apps import AppConfig
from django_logic import ProcessManager


class InvoicingConfig(AppConfig):
    name = 'invoicing'

    def ready(self):
        # Import inside ready() — never at module top in apps.py.
        from .models import Invoice
        from .process import MyProcess
        ProcessManager.bind_model_process(Invoice, MyProcess, state_field='my_state')
```

Then drive it from request/task/method bodies via `invoice.process.<action>(...)`
— never at module-import time or in another app's `ready()`.

> Make sure the app is wired so `ready()` runs — list it in `INSTALLED_APPS`
> (Django auto-discovers the single `AppConfig` in `apps.py`).


### 5. Advance your process with conditions, side-effects, and callbacks
Use next_transition to automatically continue the process.

> `next_transition` chains from a completed state change. A synchronous `Action`
> changes no state, so it ignores `next_transition`. A `BackgroundAction` does
> run it, from the worker. For a synchronous `Action`, start the follow-up from a
> callback instead, or use a `Transition`.

The four transition types do **not** share one lock/gate/chain contract.
Until 1.0 unifies them, this table is the reference:

| | Writes target state | Cache lock on success | Gates on an in-flight row | Runs `next_transition` |
|---|---|---|---|---|
| `Transition` (sync) | yes | yes | yes | yes |
| `Action` (sync) | no | no | no | **no** |
| `BackgroundTransition` | yes | yes | yes | yes (worker) |
| `BackgroundAction` | no | yes | yes | yes (worker) |

A `BackgroundAction` subclasses `BackgroundTransition`, so it locks, raises
`AlreadyInProgress` while a row is in flight, and chains — everything a sync
`Action` does not.

```python 
# Define permission and condition functions
def is_accountant(instance, user):
    return user.groups.filter(name='accountants').exists()

def is_customer_active(instance):
    return instance.customer.is_active if hasattr(instance, 'customer') else True

def generate_pdf_invoice(instance, **kwargs):
    # Generate PDF logic here
    pass

def send_approved_invoice_email_to_accountant(instance, **kwargs):
    # Send email logic here
    pass

def make_payment(instance, **kwargs):
    # Payment processing logic here
    pass

def send_void_invoice_email_to_accountant(instance, **kwargs):
    # Send void notification logic here
    pass

class MyProcess(BaseProcess):
    process_name = 'my_process' 
    permissions = [
        is_accountant, 
    ]
    transitions = [
        Transition(
            action_name='approve',
            sources=['draft'], 
            target='approved',
            conditions=[
                is_customer_active, 
            ],
            side_effects=[
                generate_pdf_invoice, 
            ],
            callbacks=[
                send_approved_invoice_email_to_accountant, 
            ],
            next_transition='pay' 
        ),
        Transition(
            action_name='pay',
            sources=['approved'],
            target='paid',
            side_effects=[
                make_payment, 
            ]
        ),         
        Transition(
            action_name='void', 
            callbacks=[
                send_void_invoice_email_to_accountant
            ],
            sources=['approved'],
            target='void'
        ),
        Action(
            action_name='update', 
            sources=['draft', 'approved'],
            side_effects=[
                update_data
            ],
        ),
    ]
```

### 6. Business logic explanation
This approval process holds these business rules:
- The user who performs the action must have the accountant role (permission).
- You cannot invoice an inactive customer (condition).
- When the invoice is approved, generate a PDF file and email it to an
accountant (side-effects and callbacks).
- When the invoice is voided, notify the accountant.

These business rules do not know about each other. You can also test every
function on its own, because Django-Logic connects them into the business
process for you.

### 7. Declarations are specifications

A process declaration is read by the person asking "how does this process
behave?" — not "how is this code structured?". Write it for that reader:

- **Write every process and every transition out in full.** Explicit
  `sources`, `target`, `conditions`, `side_effects`, `callbacks` per
  declaration — even when sibling processes repeat the same shape.
- **Duplication in declarations is acceptable, and usually preferable.**
  That is abnormal for ordinary code, and deliberate here: repeated
  declarations tell how the *process* works; a builder that assembles
  transitions tells how the *code* works, and hides the one line the
  reader came to check.
- **The same rule applies where the process is used.** Code that runs a
  process reads `instance.process.approve(...)` literally — not
  `getattr(process, action_name)` with the name held in a variable, and
  not a wrapper that hides which transition runs.

```python
# Hard to review: what are refund's sources? Which hooks run on cancel?
transitions = [_build_money_transition(name, hooks) for name, hooks in TABLE]

# Reviewable: each behaviour is stated where the reader looks for it.
transitions = [
    Transition(action_name='refund', sources=['paid'], target='refunded',
               side_effects=[reverse_charge], callbacks=[notify_customer]),
    Transition(action_name='cancel', sources=['draft', 'approved'], target='cancelled',
               side_effects=[release_stock]),
]
```

### 8. Execute in the code
```python
from invoices.models import Invoice


def approve_view(request, pk):
    invoice = Invoice.objects.get(pk=pk)
    # Check available transitions
    available_actions = invoice.my_process.get_available_actions(user=request.user)
    
    if 'approve' in available_actions:
        invoice.my_process.approve(user=request.user, context={'my_var': 1})
```
Use context to pass data between side-effects and callbacks.

> ⚠️ **django-logic checks permissions only when you pass `user=`.** A call
> without it (`invoice.my_process.approve()`) is a *system call*, and it
> **skips every permission check** by design. That is what you want in a worker
> task or a management command. It is dangerous when you forget it in an API
> view. In a request handler, always pass `user=request.user`.

### 9. Handle state field overrides
If you want to override the value of the state field, it must be done explicitly. For example: 
```python
Invoice.objects.filter(my_state='draft').update(my_state='approved')
# or 
invoice = Invoice.objects.get(pk=pk)
invoice.my_state = 'approved'
invoice.save(update_fields=['my_state'])
```
When you change the state field by hand, always pass `update_fields=['my_state']`, as shown above. django-logic writes the state the same way. A transition therefore touches only the state column, and it never overwrites a field that a side-effect changed. Follow the same pattern in your own code. A plain `instance.save()` still saves the field like any other field — django-logic does not intercept it.

### 10. Error handling
```python 
from django_logic.exceptions import TransitionNotAllowed

try:
    invoice.my_process.approve()
except TransitionNotAllowed as e:
    logger.error(f'Approve is not allowed: {e}') 
```

## Complete Example

Here's a complete working example of an order processing system:

```python
# models.py
from django.db import models
from django.contrib.auth.models import User

class Order(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending', 'Pending Payment'),
        ('paid', 'Paid'),
        ('processing', 'Processing'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
        ('refunded', 'Refunded'),
    ]
    
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='draft')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    is_paid = models.BooleanField(default=False)
    shipping_address = models.TextField(blank=True)
    tracking_number = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class Product(models.Model):
    name = models.CharField(max_length=100)
    stock = models.PositiveIntegerField(default=0)

class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)

# conditions.py
#
# A condition that raises propagates out of the action call and out of
# get_available_actions(). Keep conditions cheap: read fields that always
# exist, and return False for a missing value instead of raising.
def has_stock_available(instance):
    return all(item.product.stock >= item.quantity for item in instance.items.all())

def is_payment_verified(instance):
    return instance.is_paid

def has_shipping_address(instance):
    return bool(instance.shipping_address)

# permissions.py
def is_customer(instance, user):
    return instance.user == user

def is_staff_member(instance, user):
    return user.is_staff

# side_effects.py
def reserve_stock(instance, **kwargs):
    for item in instance.items.all():
        item.product.stock -= item.quantity
        item.product.save()

def process_payment(instance, **kwargs):
    # Payment gateway integration
    instance.is_paid = True
    instance.save()

def generate_tracking_number(instance, **kwargs):
    import uuid
    instance.tracking_number = f"TRACK-{uuid.uuid4().hex[:8].upper()}"
    instance.save()

def send_order_confirmation_email(instance, **kwargs):
    # Send email to customer
    pass

def send_shipping_notification(instance, **kwargs):
    # Send tracking info to customer
    pass

# process.py
from django_logic import Process, Transition

from .conditions import has_stock_available, has_shipping_address, is_payment_verified
from .permissions import is_customer, is_staff_member
from .side_effects import (
    generate_tracking_number,
    process_payment,
    reserve_stock,
    send_order_confirmation_email,
    send_shipping_notification,
)

class OrderProcess(Process):
    process_name = 'order_process'
    
    transitions = [
        Transition(
            action_name='submit',
            sources=['draft'],
            target='pending',
            conditions=[has_stock_available, has_shipping_address],
            side_effects=[reserve_stock],
        ),
        Transition(
            action_name='pay',
            sources=['pending'],
            target='paid',
            side_effects=[process_payment],
            callbacks=[send_order_confirmation_email],
            next_transition='process',
        ),
        # The automatic follow-up that 'pay' chains into. It declares no
        # permission on purpose. next_transition forwards the original
        # caller's user, so a staff-only permission here would stop the chain
        # for the customer who paid, and django-logic swallows a rejected
        # follow-up. Its condition is the payment check that 'pay' has just
        # satisfied.
        Transition(
            action_name='process',
            sources=['paid'],
            target='processing',
            conditions=[is_payment_verified],
        ),
        Transition(
            action_name='ship',
            sources=['processing'],
            target='shipped',
            permissions=[is_staff_member],
            side_effects=[generate_tracking_number],
            callbacks=[send_shipping_notification],
        ),
        Transition(
            action_name='deliver',
            sources=['shipped'],
            target='delivered',
        ),
        Transition(
            action_name='cancel',
            sources=['draft', 'pending'],
            target='cancelled',
            permissions=[is_customer],
        ),
        Transition(
            action_name='refund',
            sources=['paid', 'processing', 'shipped', 'delivered'],
            target='refunded',
            permissions=[is_staff_member],
        ),
    ]

# apps.py — bind in AppConfig.ready() (the one supported place; see "Bind the
# process"). Never bind at module import time.
from django.apps import AppConfig
from django_logic import ProcessManager

class ShopConfig(AppConfig):
    name = 'shop'

    def ready(self):
        from .models import Order
        from .process import OrderProcess
        ProcessManager.bind_model_process(Order, OrderProcess, state_field='status')

# views.py
from django.shortcuts import render, redirect
from django.contrib import messages
from django_logic.exceptions import TransitionNotAllowed

from .models import Order

def submit_order(request, order_id):
    order = Order.objects.get(pk=order_id, user=request.user)
    
    try:
        order.order_process.submit(user=request.user)
        messages.success(request, 'Order submitted successfully!')
    except TransitionNotAllowed as e:
        messages.error(request, f'Cannot submit order: {str(e)}')
    
    return redirect('order_detail', order_id=order.id)
```

## Troubleshooting

### Common Issues

#### 1. TransitionNotAllowed Exception
django-logic raises this exception for two different reasons. A generic handler
should answer them differently:

**Permanent refusal** — retrying is pointless:
- The current state is not in the transition's source states
- Conditions are not met
- User doesn't have required permissions
- State is already locked by another process

**Transient concurrency** — the action is fine, retry shortly. These raise
`TransitionTemporarilyUnavailable`, a `TransitionNotAllowed` subclass you can
import from `django_logic.exceptions`. You get it in two cases: another
transition owns the instance right now (`AlreadyInProgress`, or the synchronous
gate while a background row is still being retried), or the state moved while
enqueue waited (`SourceStateChanged`). A stranded row — nothing is retrying it —
does not count as busy. The synchronous gate and a later enqueue then raise the
plain base class again, so "retry shortly" is never a forever answer. An attempt
that still runs inside its declared `timeout=` budget always counts as being
retried, and so does a row a worker holds right now: before the gate answers
"stranded" it probes the row lock, so a long quiet attempt reads as busy, not
lost. Catch the transient type **ahead of** the base class:

```python
from django_logic.exceptions import (
    TransitionNotAllowed,
    TransitionTemporarilyUnavailable,
)

try:
    order.process.submit(user=request.user)
except TransitionTemporarilyUnavailable:
    return Response(status=409, data={'detail': 'Busy — please retry shortly.'})
except TransitionNotAllowed:
    return Response(status=400, data={'detail': 'Action not allowed.'})
```

**Solution**: Check available transitions using `get_available_actions()` before calling a transition, and handle the transient subclass separately.

#### 2. State Not Updating
If the state field is not updating:
- Ensure you're not using `save()` without `update_fields`
- Check if the transition completed successfully
- Verify side effects didn't raise exceptions

**Solution**: Always use `update_fields=['state_field_name']` when manually saving state changes.

#### 3. Race Conditions
Multiple processes trying to transition the same object can cause race conditions.

**Solution**: Django-Logic serializes work on a state field with two mechanisms (see [Concurrency and locking](#concurrency-and-locking)):
- a **cache lock** — an atomic set-if-absent on the `default` cache. It is held for a synchronous transition's whole run, and for the critical section of a background transition's enqueue. Both re-read the persisted state under the lock.
- the **`TransitionMessage` row**. While a background transition is in progress, a second one raises `AlreadyInProgress`, and a synchronous transition on the same instance + process raises `TransitionTemporarilyUnavailable`. Both are `TransitionNotAllowed` subclasses.

Use a cross-process cache, so that the web processes and the workers share the lock.

#### 4. Side Effects Not Rolling Back
A side-effect that changes an external system does not roll back on its own.

**Solution**: Compensate in failure callbacks. They run after django-logic unlocks the state, so make them idempotent — another process can act on the instance in between:

```python
def compensate_payment(instance, exception, **kwargs):
    # Reverse the payment if a side-effect failed
    pass

Transition(
    action_name='pay',
    sources=['pending'],
    target='paid',
    side_effects=[process_payment, another_side_effect],
    failure_callbacks=[compensate_payment, notify_admin],   # run after unlock
)
```

When a side-effect fails, django-logic runs these steps in order: write `failed_state` (when you declare one), unlock, then run the **failure_callbacks**.

## Advanced Features

### Nested Processes
Build complex workflows by combining processes:

```python
class PaymentProcess(Process):
    transitions = [
        Transition('validate', sources=['pending'], target='validated'),
        Transition('charge', sources=['validated'], target='charged'),
    ]

class OrderProcess(Process):
    nested_processes = [PaymentProcess]
    transitions = [
        Transition('submit', sources=['draft'], target='pending'),
        # ... other transitions
    ]
```

### Custom State Classes
Extend the State class for custom behavior:

```python
from django_logic.state import State

class AuditedState(State):
    def set_state(self, state):
        # Log state changes
        audit_log.create(
            model=self.instance.__class__.__name__,
            instance_id=self.instance.pk,
            field=self.field_name,
            old_value=self.get_persisted_state(),
            new_value=state,
        )
        super().set_state(state)
```

Install it with `state_class` on the process — without this the subclass is never used:

```python
class OrderProcess(Process):
    state_class = AuditedState
    transitions = [...]
```

Every state read and write for that process then goes through your class. That includes the writes the background engine makes: `in_progress_state` at enqueue, and the target state and `failed_state` on the worker. Two rules apply. `get_persisted_state()` must keep reading the database row, because the re-read under the lock and the worker's state guard trust it — never return a cached value from it. And `set_state` must call `super()`.

### Context Passing
Pass data between side effects and callbacks:

> **Reserved kwarg names.** The engine sets `tr_id`, `root_id`, `parent_id`,
> `process_class` and `owning_process_class` on every run. They identify the
> transition and its parents, and the engine forwards them to a
> `next_transition` follow-up. The engine also replaces `user` with `user_id`
> when it sends the transition to the queue, and it rebuilds `context` on the
> worker. If you pass one of those names as your own data, the engine overwrites
> it. Use different names.

> `context` lives for **one execution** only; django-logic never saves it. A
> `context=` you pass reaches the hooks of a synchronous transition. For a
> *background* transition, enqueue drops it and the worker builds an empty one.
> It carries data between the hooks of one run, not across the queue. Anything
> the worker must see belongs in ordinary kwargs, which the engine serializes,
> or on the instance itself.


```python
def calculate_total(instance, context, **kwargs):
    total = sum(item.price for item in instance.items.all())
    context['total'] = total

def apply_discount(instance, context, **kwargs):
    total = context.get('total', 0)
    instance.final_amount = total * 0.9  # 10% discount
    instance.save()

Transition(
    action_name='checkout',
    sources=['cart'],
    target='pending',
    side_effects=[calculate_total, apply_discount],
)
```

## Background Transitions

For long-running side-effects (payment processing, PDF generation, external API calls), use `BackgroundTransition` / `BackgroundAction` from `django_logic.background`. **Workers claim committed rows straight from the database** — `'pull'` is the default execution mode, and no broker is involved.

**How the work is split.** A synchronous `Transition` does everything at once, in the caller's call frame. A background transition cannot: its work runs later, on another machine. It therefore follows the standard transactional-outbox pattern, in two steps:

- **Enqueue** (synchronous, inside your request): validate the transition, then write `in_progress_state` and a durable `TransitionMessage` row in **one** database transaction. The row records what you asked for and is the only signal there is — a payload-free notification wakes the workers on commit. This takes milliseconds.
- **Execute** (on a worker process): a worker claims the row (`SELECT FOR UPDATE SKIP LOCKED`), runs the side-effects, writes the target state and marks the row completed — all in one atomic block. A failed attempt becomes claimable again after the retry wait; a crashed worker's row is claimable the moment its connection dies. Success and failure *callbacks* run after the worker's transaction commits, and they are best-effort by contract: a worker killed between that commit and the callbacks loses them, and nothing re-runs them. So a callback that applies a decision the side-effect recorded (disable this account, mark that parcel failed) needs a periodic re-check behind it — or make the follow-up its own `BackgroundTransition`, which gets its own row and its own retries.

They give you:

- **Durable execution.** django-logic saves every background transition as a `TransitionMessage` row, in the same atomic block that writes `in_progress_state`. The row is what workers run, so there is no message to lose: a crash, a missed notification, or a worker outage only delays the claim.
- **Queue routing per transition.** `queue=` is optional — a transition without it runs on `DJANGO_LOGIC['DEFAULT_QUEUE']` (`'django_logic'`). Name your queues per SLA (`critical` / `slow` / `fast`) and give each one its own worker.
- **Sync mode for tests.** `'sync'` runs the worker path inline, in the same process — for unit tests, CI, management commands and the Django shell. You need no services to test a business process; see [Testing Your Processes](#testing-your-processes).
- **One attempt, all or nothing.** Every side-effect and the target-state write happen inside **one** atomic block, with the side-effects in a savepoint. A failed attempt **rolls back every database write it made**, and the whole attempt re-runs on the next claim, so the state never stops between two side-effects. The idempotency you owe is for *external* calls only, because a retried attempt runs the side-effects again from the start.

### Install

Add `'django_logic.background'` to `INSTALLED_APPS` and configure:

```python
DJANGO_LOGIC = {
    'LOCK_TIMEOUT': 7200,   # the state lock's TTL, seconds
    'BACKGROUND_EXECUTION': 'pull',     # the default; set 'sync' in test settings
    'DEFAULT_QUEUE': 'django_logic',    # queue for transitions without queue=
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
    'STRICT_KWARGS_SERIALIZATION': False,  # True: raise (not warn) on dropped 'request' / non-string dict keys
    'STRICT_HOOK_SIGNATURES': False,    # True: refuse to bind hooks without a named instance-first parameter
    'DEFER_UNLOCK_UNTIL_COMMIT': False,  # True: sync unlocks ride transaction.on_commit (see "Concurrency and locking")
    # 'LEGACY_EXCEPTION_BASE': '...',  # opt-in: dotted path of a fork's TransitionNotAllowed to mix in during a migration (see below)
}
```

Every key has the default shown above, so an empty `DJANGO_LOGIC = {}` is a valid production start. `manage.py check` reports a key the engine does not read as `django_logic.W004` instead of ignoring it — that catches a typo and a key a past release removed. django-logic also validates the numeric and safety-critical settings at boot: a bad value raises `ImproperlyConfigured` and names the setting, so you find out before a worker fails on it. Run `manage.py migrate` to create the `TransitionMessage` table.

#### Migrating off a fork: `LEGACY_EXCEPTION_BASE`

A project that migrates off a fork of this library runs both engines side by
side while its apps move one at a time. Its shared handlers — DRF mixins, admin
actions, Sentry ignore lists — catch the *fork's* `TransitionNotAllowed`. Name
the fork's class here, and this engine's refusals become instances of it too, so
those handlers keep working:

```python
DJANGO_LOGIC = {
    'LEGACY_EXCEPTION_BASE': 'old_fork.exceptions.TransitionNotAllowed',
}
```

django-logic adds the class to `TransitionNotAllowed`'s bases in
`AppConfig.ready()`, so every subclass is covered, including
`TransitionTemporarilyUnavailable`. The setting costs nothing when you leave it
out. Every way it can go wrong — a path that does not import, a class that is
not an exception, an MRO conflict — raises `ImproperlyConfigured` at boot.
Remove the setting when the migration is done.

At boot, pull mode refuses two settings that would break the guarantees without saying so. The first is a SQLite database for `TransitionMessage`, which has no row locks for the claim. The second is a per-process `default` cache (locmem or dummy) when `DEBUG=False`, because the web processes and the workers must share the state lock:

```python
CACHES = {
    'default': {
        # Built into Django since 4.0; needs the `redis` client package.
        # `django_redis.cache.RedisCache` also works — `pip install django-logic[redis]`.
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': os.environ['REDIS_URL'],
    }
}
```

### Declare a background transition

```python
from django_logic import Process, Transition
from django_logic.background import BackgroundTransition, BackgroundAction


class OrderProcess(Process):
    transitions = [
        Transition(
            action_name='approve',
            sources=['draft'],
            target='approved',
            side_effects=[validate_order],
        ),
        BackgroundTransition(
            action_name='fulfil',
            sources=['approved'],
            target='fulfilled',
            in_progress_state='fulfilling',
            failed_state='fulfilment_failed',
            queue='django_logic.critical',     # explicit queue: dedicated worker, tight SLA
            side_effects=[reserve_stock, generate_labels, call_courier],
            callbacks=[send_confirmation_email],
        ),
        BackgroundTransition(
            action_name='generate_export',
            sources=['fulfilled'],
            target='exported',
            in_progress_state='exporting',
            failed_state='export_failed',
            queue='django_logic.slow',         # slow work, isolated worker
            side_effects=[build_csv, upload_to_s3],
        ),
        BackgroundAction(
            action_name='sync_inventory',
            sources=['fulfilled'],
            # no queue= — runs on DEFAULT_QUEUE ('django_logic')
            side_effects=[push_to_erp],
        ),
    ]


# apps.py — bind in AppConfig.ready() (the one supported place; see "Bind the process").
from django.apps import AppConfig
from django_logic import ProcessManager

class ShopConfig(AppConfig):
    name = 'shop'

    def ready(self):
        from .models import Order
        from .process import OrderProcess
        ProcessManager.bind_model_process(Order, OrderProcess, state_field='status')
```

### Call it

```python
# In a view — returns immediately (Pull mode) or after the worker completes (Sync mode).
tr_id = order.process.fulfil(user=request.user)
```

### Say a failure is permanent

The worker retries every side-effect failure until `MAX_ERRORS`, because a
lost connection or a timeout may pass on the next attempt. A refusal does
not: no record matched, a rule said no, the payload was rejected. Retrying
one only delays the answer by `RETRY_MINUTES × MAX_ERRORS`. Say so, and the
worker takes the terminal path on the first attempt — it writes
`failed_state`, runs `failure_callbacks`, and completes the row, exactly as
an exhausted retry does:

```python
from django_logic.background import PermanentFailure

def match_order(instance, **kwargs):
    order = find_order(instance.barcode)
    if order is None:
        raise PermanentFailure('no order matches this barcode')
    ...
```

For an exception type you do not control, declare it on the transition:

```python
BackgroundTransition(
    action_name='submit_declaration',
    sources=['packed'],
    target='declared',
    failed_state='declaration_refused',
    side_effects=[submit_declaration],
    no_retry_on=(CustomsRefusal,),
)
```

The two compose: raise `PermanentFailure` from code you own, list the types
you do not. Everything else keeps its retries.

### Polymorphic routing with nested processes

Nested processes let several sub-processes share one `action_name`. A
**condition on the instance** then picks one of them at run time, so a generic
caller invokes one method and the right implementation runs. This works for
background transitions too: each integration keeps its durable work on its own
nested process, and callers never have to know which one.

```python
def is_gmail(conversation, **kw):  return conversation.source_integration == 'gmail'
def is_dummy(conversation, **kw):  return conversation.source_integration == 'dummy'

class GmailConversationProcess(Process):
    process_name = 'gmail_conversation'
    transitions = [
        BackgroundTransition(
            action_name='send_message_via_integration',
            sources=['open'], target='open',
            in_progress_state='gmail_sending',
            conditions=[is_gmail],
            side_effects=[send_via_gmail],
        ),
    ]

class DummyConversationProcess(Process):
    process_name = 'dummy_conversation'
    transitions = [
        BackgroundTransition(
            action_name='send_message_via_integration',   # same name, different owner
            sources=['open'], target='open',
            in_progress_state='dummy_sending',
            conditions=[is_dummy],
            side_effects=[send_via_dummy],
        ),
    ]

class ConversationProcess(Process):
    nested_processes = [GmailConversationProcess, DummyConversationProcess]

# apps.py — bind in AppConfig.ready() (the one supported place; see "Bind the process").
from django.apps import AppConfig
from django_logic import ProcessManager

class MessagingConfig(AppConfig):
    name = 'messaging'

    def ready(self):
        from .models import Conversation
        from .process import ConversationProcess
        ProcessManager.bind_model_process(Conversation, ConversationProcess, state_field='status')

# Generic caller — routes by source_integration, no integration knowledge here:
conversation.process.send_message_via_integration(user=request.user)
```

Enqueue resolves exactly one transition, because the conditions exclude each
other. It records the **nested process class that declared the transition** on
the `TransitionMessage`. The worker restores that exact transition from the
recorded class and does not evaluate the condition again, so the routing holds
even when the instance changes while the row waits. One constraint: a background
`action_name` must be **unique within a single process class**, because the
worker cannot tell two transitions of the same name in one class apart. You may
share `in_progress_state` freely, which helps when a UI knows only one "busy"
value. Every marked instance carries its exact transition on the
`TransitionMessage` row, so recovery never has to guess which transition owns it.
(0.12.0 retired the `django_logic.E001` check that used to forbid sharing, along
with the stranded sweep.) A background `action_name` may also match a synchronous
transition of the same name: the worker restores background transitions only, and
enqueue routes the call by condition. A synchronous fast path and a durable
background slow path can therefore share one `action_name`.

> **`in_progress_state` is background-only (0.12.0).** On a `BackgroundTransition`
> django-logic writes the state in the same transaction as the
> `TransitionMessage` row, so a row owns the recovery of every marked instance. A
> *synchronous* transition used to write the state under a cache lock with no
> durable row. A killed process then left the instance parked in a state with no
> way out, and the engine needed a whole sweeping subsystem
> (`recover_stranded_states`, now retired) to find those instances. Declaring the
> state on a plain `Transition`/`Action` now raises `ImproperlyConfigured`.
> Without that write, a killed synchronous run rolls back to its source state and
> you can run it again once the lock TTL expires. There is nothing to sweep.
>
> **Migrating a sync transition that used the marker:** model the busy step as
> a real state with explicit edges —
>
> ```python
> Transition('submit', sources=['draft'], target='fulfilling',
>            next_transition='do_fulfil'),
> BackgroundTransition('do_fulfil', sources=['fulfilling'], target='fulfilled',
>                      failed_state='fulfilment_failed', side_effects=[...]),
> ```
>
> Readers still see `fulfilling`, and a `TransitionMessage` row now owns the
> work. The pattern has one narrow window that the old atomic write did not have.
> A crash between `submit`'s commit and the chained dispatch parks the instance at
> `fulfilling` with no row. A three-line periodic retry recovers it, and the retry
> is safe: an instance that is really in progress raises `AlreadyInProgress` and
> is skipped, while a parked one moves *forward*:
>
> ```python
> for obj in Order.objects.filter(status='fulfilling',
>                                 modified__lt=now() - timedelta(hours=1)):
>     try:
>         obj.process.do_fulfil()
>     except TransitionNotAllowed:
>         pass  # in progress or locked — someone owns it
> ```

> **Upgrade note.** You may turn an existing background transition with a unique
> name into this shared-name nested pattern. Deploy that change with no
> uncompleted rows for the action, or split it across two deploys. A row that
> older code enqueued does not name the process that declared the transition.
> Once several nested processes share the name, the worker cannot tell which one
> such a row meant, so it completes the row without running the side-effects. That
> is safe, but the work does not run. Every row enqueued after the upgrade records
> the process that declared it.

### Testing background transitions

Set `BACKGROUND_EXECUTION='sync'` in your test settings. The global default is `'pull'`, so you must opt in. Every `instance.process.fulfil(...)` call then enqueues **and** executes inline:

```python
class FulfilmentTests(TestCase):
    def test_happy_path(self):
        order = Order.objects.create(status='approved')
        order.process.fulfil()
        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')

    def test_side_effect_failure_propagates(self):
        # Patch what the side-effect CALLS, not the side-effect itself. The
        # Transition captured the function object when the class was defined,
        # so patching its module attribute does not replace it.
        # django_logic.testing's fail_side_effect= avoids this trap.
        order = Order.objects.create(status='approved')
        with patch('myapp.services.courier_client.book', side_effect=CourierError):
            with self.assertRaises(CourierError):
                order.process.fulfil()
```

If the global setting is `'pull'` but you need Sync mode for a specific block, use the context manager:

```python
from django_logic.background import sync_execution

with sync_execution():
    order.process.fulfil()
```

### Suggested queue layout

```
django_logic.fast       — < 1s work (notifications, cache invalidations)
django_logic.critical   — user-facing with SLA (fulfilment, payments)
django_logic.slow       — > 30s work (exports, reports)
```

A queue is a column on the row, and each worker process names the queues it serves — so a retried slow job never moves to the critical worker.

### Retries, and the safety nets

Retries need no scheduler: a row whose attempt failed becomes claimable again after `RETRY_MINUTES` — the claim's own filter is the retry rule. Three safety nets run inside every worker loop, once a minute, so nothing else has to be configured:

- `detect_stuck_transitions` — finalizes a row that sits at `MAX_ERRORS`: it writes `failed_state`, runs `failure_callbacks` and marks the row completed, so the retry loop stops. It also names every row that has waited past the retry window with no attempt ever started — the sign that no worker serves that row's queue.
- `cleanup_completed_transitions` — deletes completed rows older than `CLEANUP_DAYS`, except the newest terminal-failure row per instance and process. That row is the only explanation for an instance parked in its `failed_state`, so it stays for the investigation, however late it comes.

### Per-attempt timeouts

A `BackgroundTransition` (or a `BackgroundAction`) may give each attempt a wall-clock budget with `timeout=<seconds>`:

```python
BackgroundTransition(
    action_name='generate_export',
    sources=['fulfilled'],
    target='exported',
    in_progress_state='exporting',
    failed_state='export_failed',
    queue='django_logic.slow',
    timeout=600,                       # give up on an attempt after 10 minutes
    side_effects=[build_csv, upload_to_s3],
)
```

The worker enforces the budget: every attempt runs in its own forked attempt process, and when it runs past `timeout`, the worker kills it. The kill releases the attempt's row lock with its connection, the worker records one `[timeout]` error on the row, the claim's retry wait paces the next attempt, and at `MAX_ERRORS` the stuck finalizer ends the row in `failed_state`. A row without `timeout` is unbounded. Enforcement exists only where an attempt process exists: in sync mode the attempt runs in the caller's own thread and no budget is enforced. A killed attempt's database writes roll back with it, but an external API call it already made has happened. **Side-effects must be idempotent against external systems.**

### Concurrency and locking

Two mechanisms serialize work on a state field, each with its own scope:

1. **The cache lock** — an atomic set-if-absent on the `default` cache. A *synchronous* transition holds it for its whole run. A background transition holds it for the **critical section of enqueue only**: validate, create the `TransitionMessage`, write `in_progress_state`, release. Both re-read the **persisted** state under the lock before they go on, so two requests that race on the same instance cannot both win.
2. **The uncompleted `TransitionMessage` row** gates concurrent background work. While one exists for an instance + process:
   - a second background transition raises `AlreadyInProgress` (`from django_logic.background.exceptions import AlreadyInProgress`). You can also catch it as `TransitionTemporarilyUnavailable` from `django_logic.exceptions`, without importing the background subpackage. A partial unique constraint enforces this, so it holds across processes and machines.
   - a **synchronous transition on the same instance + process raises `TransitionTemporarilyUnavailable`**, because the worker owns the state field until the row completes.
   - a synchronous `Action` still runs, because it changes no state on success. django-logic skips a failing Action's `failed_state` write while the row is uncompleted, for the same reason.

The constraint is scoped **per process**. Two independent state machines bound to different fields of the same model — say `status` and `payment_status` — can both have background work in progress.

**Data-dependent outcomes ("verdicts").** A background side effect cannot run a synchronous transition on its own instance: its own row is still uncompleted, so the gate above refuses it. When the side effect's result decides the next state (a validation that ends valid or invalid), write the decided call explicitly and run it from the transition's `callbacks`, which execute after the row completes:

```python
from functools import partial

def fetch_sheet(import_obj, **kwargs):
    rows = read_sheet(import_obj)
    if all(row.valid for row in rows):
        import_obj.run_verdict = partial(import_obj.process.mark_as_valid, data=rows)
    else:
        import_obj.run_verdict = partial(import_obj.process.mark_as_invalid,
                                         errors=errors_of(rows))

def apply_verdict(import_obj, **kwargs):
    run_verdict = import_obj.__dict__.pop('run_verdict', None)
    if run_verdict is not None:
        run_verdict()

BackgroundAction(action_name='run_validation', sources=['validating'],
                 side_effects=[fetch_sheet], callbacks=[apply_verdict])
```

Each decision point names the exact transition it runs. Callbacks are best-effort: a worker that dies between completing the row and the callback loses the verdict. The instance stays in its current state, and the operator can run the action again — nothing is corrupted.

To answer "busy, try again shortly" in your own API, read the row through `in_flight()` instead of writing the filter yourself:

```python
from django_logic.background import in_flight

if in_flight(order, 'process'):
    return Response(status=409, data={'detail': 'Busy — please retry shortly.'})
```

The answer can go out of date at once, because a transition can start or complete right after the read. Use it to shape an answer, not as a gate — the engine's own guards decide. It answers the *busy* question only: for a stranded row (uncompleted, nothing retrying it) it returns `False`, which matches the plain `TransitionNotAllowed` the engine's gates raise for such a row.

The gate is a database row, not a held lock, so nothing is left behind when the caller's surrounding transaction rolls back. The row, the `in_progress_state` write and the dispatch all disappear together.

**Lock ownership.** Every acquisition stores a unique token, and the release compares the token before it deletes the key. A synchronous run that outlives its lock TTL therefore cannot delete the lock that a later run acquired: the token does not match, so it leaves the lock alone and returns. A `State` object that never locked still deletes the key without a check, which gives you a way to release a lock by hand.

**Synchronous transitions inside an outer `transaction.atomic()`.** By default django-logic releases the lock as soon as the transition completes, before the outer block commits. That window is real. Another connection can take the lock, read the *old committed* state, and run the same transition again. Both runs then execute the side-effects, and the final state depends on which one commits last. Opt in when your code drives transitions inside atomic blocks and needs the exclusion to cover the whole uncommitted span:

```python
DJANGO_LOGIC = {..., 'DEFER_UNLOCK_UNTIL_COMMIT': True}
```

The unlock then runs from `transaction.on_commit`. Design for two trade-offs. On **rollback** the hook never runs, so the lock waits for its TTL to expire — a lockout with a known end, the same as after a crashed process. And a follow-up on the same instance (`callbacks` / `next_transition`) inside the atomic block finds the state still locked, so django-logic skips it; start those from `transaction.on_commit` in the caller instead. You can also keep the default and call the transition from `transaction.on_commit`, so it starts only once the surrounding write is visible.

One consequence: you **cannot** chain a background transition from another transition's `callbacks` or `next_transition` on the *same* instance while the first row is still uncompleted. The chained enqueue raises `AlreadyInProgress`. Start follow-up background work from a terminal hook — a success or failure callback that runs after django-logic marks the first row completed — or work on a different instance.

> ⚠️ **Do not treat `AlreadyInProgress` as "already queued, my changes will be picked up".** That is only true while the existing attempt has **not started**. If the worker already runs, it has already read its inputs. Your update lands after that read, the run commits a result computed from the older data, and nothing runs again. For a recompute-style transition, save a dirty flag (or a version) *before* you dispatch, clear it inside the side-effect, and dispatch again from a success callback when the flag is set again:
>
> ```python
> def recompute(instance, **kwargs):
>     Order.objects.filter(pk=instance.pk).update(recompute_requested=False)
>     ...  # compute from current rows
>
> def redispatch_if_dirty(instance, **kwargs):   # success callback (terminal)
>     instance.refresh_from_db()
>     if instance.recompute_requested:
>         instance.process.recompute_rates()
> ```

### The worker's state guard

The worker restores the transition by name and skips the source-state check on purpose. Something else can still move the instance while the row waits: an operator fix in the admin, a data migration, a support script. Retries span `RETRY_MINUTES × MAX_ERRORS`, so that collision does happen in production.

Before it runs the side-effects, the worker checks that the persisted state still matches what enqueue left behind — `in_progress_state`, or a declared source state when the transition has no `in_progress_state`. If it does not match, the worker completes the row as **superseded**: it skips the side-effects, the external state change wins, and it records the reason on the row (`last_error_message` starts with `[superseded]`) and logs it at ERROR.

The same check guards the `failed_state` writes that the stuck finalizer makes, so finalizing a long-stranded row never overwrites a manual fix.

### Production deployment

Pull mode needs two things. Both are checked at boot:

**1. PostgreSQL for `TransitionMessage`.** The worker's claim is `SELECT FOR UPDATE SKIP LOCKED`; SQLite has no row locks, so boot refuses it (use `'sync'` there).

**2. One worker process per queue group.** Each worker names the queues it serves and loops: claim a row, run it, ask again. A payload-free `LISTEN/NOTIFY` wakes it the moment enqueue commits; a five-second poll is the floor, so a lost notification costs five seconds, never the work. The loop also runs the safety nets once a minute — there is nothing else to schedule, anywhere:

```bash
python manage.py dl_worker --queues django_logic.critical,django_logic.fast
python manage.py dl_worker --queues django_logic.slow
```

Crash recovery is the database's own: a worker that dies releases its row lock with its connection, and the next claim takes the row at once. An attempt that hangs while keeping its connection is stopped by its own budget — declare `timeout=` and the worker kills the attempt when it runs past it.

**Running behind pgbouncer (transaction pooling).** The concurrency guard —
`select_for_update(nowait)` plus the partial unique constraint — works under
pgbouncer **transaction** pooling. Transaction mode does not support a few
PostgreSQL session features, so set these two options:

```python
DATABASES['default'].setdefault('OPTIONS', {})['prepare_threshold'] = None  # psycopg3: no server-side prepared statements
DATABASES['default']['DISABLE_SERVER_SIDE_CURSORS'] = True
```

Also do **not** force `sslmode=require` on the app→pgbouncer connection. That
hop is local and plaintext, and pgbouncer terminates TLS upstream. Without
`prepare_threshold=None`, the worker fails or hangs from time to time with
prepared-statement errors. This setup is validated end to end on Heroku behind an
in-dyno pgbouncer.

**Monitoring.** In Pull mode django-logic logs a failed attempt (`django-logic.transition` at ERROR) and records it on the row; the worker loop keeps going. So watch the `TransitionMessage` table:

```sql
-- rows at the error limit (detect_stuck_transitions should be finalizing these)
SELECT count(*) FROM django_logic_background_transitionmessage
 WHERE is_completed = false AND errors_count >= 5;            -- = TRANSITION_MESSAGE_MAX_ERRORS

-- attempts running far longer than expected
SELECT count(*) FROM django_logic_background_transitionmessage
 WHERE is_completed = false AND started_at < now() - interval '15 minutes';

-- rows superseded by an external state change (review these now and then:
-- each one is a manual fix or an external write that won over a waiting row)
SELECT count(*) FROM django_logic_background_transitionmessage
 WHERE last_error_message LIKE '[superseded]%';
```

Also alert when the worker processes stop, because the safety nets run inside their loop and stop with them.

**Migrating an existing deployment.** Migration `0005` widens `instance_id` from integer to `varchar(255)` with `ALTER COLUMN ... TYPE`. Django emits the `USING ...::varchar` cast, so existing integer rows convert in place. On a very large `TransitionMessage` table this rewrites the column under a lock — run it in a maintenance window, or with your usual online-migration tooling. Migration `0006` (0.4.0) adds the `field_name` column. It also swaps the partial unique constraint from per-instance (`dl_bg_only_one_uncompleted_per_instance`) to per-process (`dl_bg_one_uncompleted_per_process`). That is a quick metadata and index change, safe to run in place.

## Testing Your Processes

Workflows are hard to test well, because states, conditions, permissions,
side-effects, background jobs, failures, retries and locking all interact.
`django_logic.testing` gives you a **scenario-based** test base class. A test
reads like the business process, and everything runs **inline, with no services
broker** — background transitions included.

Two principles keep these tests worth writing: **test your process, not the
machinery**, and **assert what the object became, not that a hook ran**. Full
rationale, and the scenario catalog, in
[docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md#journeys-not-mirrors).

```python
from django_logic.testing import ProcessScenario


class TestOrderFulfilment(ProcessScenario):
    """Order lifecycle: draft -> approved -> fulfilling -> fulfilled."""
    process_class = OrderProcess
    model = Order
    state_field = 'status'      # default: 'status'
    process_name = 'process'    # default: 'process'

    def test_happy_path(self):
        order = self.create_instance(status='approved')
        self.assert_available(order, ['fulfil', 'cancel'])

        self.background_transition(order, 'fulfil')      # enqueue + execute, inline
        self.assert_state(order, 'fulfilled')
        self.assert_side_effects_ran(['reserve_stock', 'call_courier'])
        self.assert_callbacks_ran(['send_confirmation_email'])

    def test_courier_failure_then_retry(self):
        order = self.create_instance(status='approved')

        # Make ONE named side-effect raise — the real failure path runs.
        self.background_transition(
            order, 'fulfil',
            fail_side_effect='call_courier',
            fail_with=ConnectionError('Aramex timeout'))

        self.assert_state(order, 'fulfilling')           # still in progress
        self.assert_error_recorded(order, 'Aramex timeout')
        self.assert_error_count(order, 1)
        self.assert_side_effects_not_ran(['call_courier'])

        self.retry_transition(order)                      # what the starter would do
        self.assert_state(order, 'fulfilled')

    def test_only_staff_can_approve(self):
        # self.staff / self.customer are your own setUp fixtures —
        # ProcessScenario does not create users.
        order = self.create_instance(status='draft')
        self.assert_available(order, ['approve'], user=self.staff)
        self.assert_not_available(order, ['approve'], user=self.customer)

    def test_approve_produces_the_right_outcome(self):
        # Assert what the object BECAME, not just that a hook ran.
        order = self.create_instance(status='draft')
        before = self.capture(order, ['status', 'approved_at'])
        self.transition(order, 'approve', user=self.staff)
        self.assert_side_effects_ran(['validate_order'])        # wiring
        self.assert_changed(order, before, {                    # outcome
            'status': ('draft', 'approved'),
            'approved_at': (None, order.approved_at),
        })

    def test_fulfil_failure_reaches_the_caller(self):
        order = self.create_instance(status='approved')
        # A failing side-effect runs the failure path AND re-raises — pin both.
        self.background_transition(order, 'fulfil',
                                   fail_side_effect='call_courier',
                                   fail_with=ConnectionError('down'),
                                   expect_raises=ConnectionError)
        self.assert_state(order, 'fulfilling')                  # still in progress
        self.assert_raised(ConnectionError, match='down')
```

**Driving the process**

| Method | What it does |
|--------|--------------|
| `create_instance(**fields)` | Create a model instance (state via the `state_field` kwarg) |
| `transition(obj, action, **kwargs)` | Run a synchronous transition |
| `background_transition(obj, action, **kwargs)` | Run a `BackgroundTransition`/`BackgroundAction` enqueue **and** execute inline |
| `retry_transition(obj)` | Re-run the instance's uncompleted transition — what a worker's next claim would do |

Add `fail_side_effect='name'` and `fail_with=SomeError(...)` to `transition`, `background_transition` or `retry_transition` to make one named side-effect raise. django-logic wraps only that side-effect, so every other one runs for real and you exercise the true failure path. Add `expect_raises=SomeError` to assert the failure **reached the caller**, which is the contract for `side_effects`. Add `expect_raises=False` to assert django-logic **swallowed** it, which is the contract for `callbacks` and `next_transition`. Leave it out to absorb the injected exception and assert on the recorded error instead.

**Assertions**

- *State & availability:* `assert_state` · `assert_state_trace` · `assert_available` / `assert_not_available` (optional `user=`).
- *Domain outcome* — what the object *became*: `capture` → `assert_changed` / `assert_unchanged` · `assert_related_count`.
- *Wiring* — that a hook ran (pair with an outcome assertion): `assert_side_effects_ran` / `assert_side_effects_not_ran` · `assert_callbacks_ran` · `assert_failure_callbacks_ran`.
- *Caller boundary & durable row:* `assert_raised` / `assert_not_raised` · `assert_error_recorded` · `assert_error_count` · `assert_transition_owner`.
- *The whole journey:* `assert_journey([JourneyStep(...)])`.

django-logic **tracks** side-effects and callbacks by function `__name__`; it does not mock them. The real code runs and the framework records what ran. `assert_side_effects_ran` and `assert_callbacks_ran` check the wiring: a hook ran, not that it did the right thing. Pair them with `assert_changed`, `assert_related_count` or `assert_state`.

**Snapshot and replay.** `snapshot(obj)` captures an instance's fields, state and
`TransitionMessage` as JSON, from a shell, an admin action, Sentry or a log.
`self.from_snapshot('fixtures/bug_12345.json')` rebuilds it in a test. That turns
a production bug into a regression test. See the snapshot scenario in
[docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md).

**Failure output an agent can read.** When an assertion fails, the error carries a numbered timeline of every step and the relevant `TransitionMessage`. A person or an agent can then see where the process went wrong without reading a stack trace.

`ProcessScenario` extends `TransactionTestCase`, so it works with the durable `TransitionMessage` row and the atomic blocks around it.

## Contributing
Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

### Development Setup

#### Option A: Local

1. Clone the repository
2. Create a virtual environment: `python -m venv venv`
3. Install dependencies: `pip install -e .`
4. Run tests: `python tests/manage.py test`

#### Option B: Docker + Make

The project includes a `Dockerfile` and a `makefile` so you can develop without installing anything locally.

```bash
make build          # build the Docker image
make test           # run the full test suite
make test-one t=tests.test_transition  # run a specific test module
make coverage       # run tests with coverage report
make sh             # open a Django shell inside the container
```

Please make sure to:
- Add tests for new features
- Update documentation
- Follow PEP 8 style guidelines
- Add type hints where applicable

## License
[MIT](https://choosealicense.com/licenses/mit/)

## Project status
Under active development. See [GitHub Issues](https://github.com/Borderless360/django-logic/issues) for planned features and known issues.

## Support
- 📖 [Documentation](https://github.com/Borderless360/django-logic/wiki)
- 🐛 [Issue Tracker](https://github.com/Borderless360/django-logic/issues)
- 💬 [Discussions](https://github.com/Borderless360/django-logic/discussions)
