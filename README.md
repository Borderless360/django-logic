![django-logic](https://user-images.githubusercontent.com/6745569/87846635-dabb1500-c903-11ea-9fae-f1960dd2f82d.png)

[![CI](https://github.com/Borderless360/django-logic/actions/workflows/ci.yml/badge.svg)](https://github.com/Borderless360/django-logic/actions/workflows/ci.yml)
[![Coverage Status](https://coveralls.io/repos/github/Borderless360/django-logic/badge.svg?branch=master)](https://coveralls.io/github/Borderless360/django-logic?branch=master)
[![License](https://img.shields.io/pypi/l/django-logic.svg)](https://github.com/Borderless360/django-logic/blob/master/LICENSE)
     
Django Logic is a lightweight workflow framework for Django that makes it easy to implement complex business logic using finite-state machines (FSM). It provides a clean, declarative way to manage state transitions, permissions, and side effects in your Django applications.

## Table of Contents
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Core Concepts](#core-concepts)
- [Usage](#usage)
- [Complete Example](#complete-example)
- [Django-Logic vs Django FSM](#django-logic-vs-django-fsm)
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
- ⏳ **Durable Background Transitions** - Background transitions run as Celery tasks by default — built in, not an optional extra. Queue-routed, retryable, self-healing (see [Background Transitions](#background-transitions))
- 🧪 **Scenario-Based Testing** - Test whole workflows — including background jobs, failures, and retries — as ordinary unit tests via sync execution mode and `django_logic.testing`, no Celery broker needed (see [Testing Your Processes](#testing-your-processes))
- 🔍 **Structured Logging** - State changes flow through the standard `django-logic` / `django-logic.transition` Python loggers, configured via Django `LOGGING` (see [docs/logger.md](docs/logger.md))

## Requirements
- Python 3.11+
- Django 4.0+
- django-model-utils >= 4.5.1
- celery >= 5.0 — **installed automatically**; background transitions are Celery tasks
- django-redis >= 5.0.0 — **installed automatically**; provides the cross-process state lock (the lock cache / `RedisState`)

Extras:
- `pip install django-logic[drf]` — pulls in `djangorestframework` (kept for projects migrating off the old DRF-coupled releases; 0.4.x ships no DRF-specific code)
- `[celery]` and `[redis]` remain as **empty aliases**, so existing `pip install django-logic[celery,redis]` pins keep resolving — both packages are core dependencies as of 0.4

## Installation

> **Heads up — versions.** The PyPI release is still the legacy `0.1.x` line.
> The 0.4.x API documented in this README ships from GitHub (`master`);
> install it from a tag until 0.4.x is published to PyPI.

```bash
# 0.4.x — the version this README documents (from GitHub).
# Celery and django-redis are installed automatically.
pip install "django-logic @ git+https://github.com/Borderless360/django-logic.git@v0.4.0"

# 0.1.x — legacy release on PyPI (different API)
pip install django-logic
```

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

# apps.py — bind the process in your app's AppConfig.ready(). This is the one
# supported place to bind: ready() runs after every app's models are loaded, so
# it avoids the model→process→actions→model circular import that binding at
# module import time (in models.py or process.py) creates. See "Bind the
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
- **Transition** - Changes the state of an object from one to another. Contains conditions, permissions, side-effects, callbacks, failure side-effects, and failure callbacks.
- **Action** - Similar to transition but doesn't change the state. Useful for operations that need permissions and side effects without state change.
- **Side-effects** - Functions executed during a transition before reaching the target state. If any fail, the state does not advance (`failed_state` is applied if declared). Background transitions additionally roll back the failed attempt's database writes (savepoint); synchronous side-effect writes are **not** rolled back automatically.
- **Callbacks** - Functions executed after successfully reaching the target state.
- **Failure side-effects** - Functions executed when side-effects fail, before the state is unlocked. Useful for cleanup or compensation that must run while the instance is still locked.
- **Failure callbacks** - Functions executed after side-effects fail, after the state is unlocked.
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

A process references its model (and its side-effect/condition/permission
functions reference it too), so binding `Model ⇄ Process` at import time forces
`models.py → process.py → actions.py → models.py` — a circular import
(issue #100). The only escape is scattering `from .models import X` calls inside
every action function. `ready()` removes the cycle entirely: Django imports
**all** apps' models before running **any** `ready()`, so by the time you bind,
every model already exists and your action modules can import the model at the
top level like normal code.

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
This approval process defines the business logic where:
- The user who performs the action must have accountant role (permission).
- It shouldn't be possible to invoice inactive customers (condition). 
- Once the invoice record is approved, it should generate a PDF file and send it to 
an accountant via email. (side-effects  and callbacks)
- If the invoice voided it needs to notify the accountant about that.

As you see, these business requirements should not know about each other. Furthermore, it gives a simple way 
to test every function separately as Django-Logic takes care of connection them into the business process.  

### 7. Execute in the code
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

> ⚠️ **Permissions are only checked when you pass `user=`.** Calling a
> transition without it (`invoice.my_process.approve()`) is treated as a
> *system call* and **bypasses all permission checks** by design — useful in
> Celery tasks and management commands, dangerous when forgotten in an API
> view. In request handlers, always pass `user=request.user`.

### 8. Handle state field overrides
If you want to override the value of the state field, it must be done explicitly. For example: 
```python
Invoice.objects.filter(my_state='draft').update(my_state='approved')
# or 
invoice = Invoice.objects.get(pk=pk)
invoice.my_state = 'approved'
invoice.save(update_fields=['my_state'])
```
When changing the state field manually, always pass `update_fields=['my_state']` (as shown above). django-logic itself writes state via `update_fields` so a transition touches only the state column and never clobbers fields a side-effect changed — follow the same pattern in your own code. (Note: a plain `instance.save()` *will* persist the field like any other; django-logic does not intercept it.)

### 9. Error handling
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
    tracking_number = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

# conditions.py
def has_stock_available(instance):
    # Check if all order items are in stock
    return all(item.product.stock >= item.quantity for item in instance.items.all())

def is_payment_verified(instance):
    return instance.is_paid

def has_shipping_address(instance):
    return hasattr(instance, 'shipping_address') and instance.shipping_address is not None

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
        Transition(
            action_name='process',
            sources=['paid'],
            target='processing',
            permissions=[is_staff_member],
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
This exception is raised when:
- The current state is not in the transition's source states
- Conditions are not met
- User doesn't have required permissions
- State is already locked by another process

**Solution**: Check available transitions using `get_available_actions()` before calling a transition.

#### 2. State Not Updating
If the state field is not updating:
- Ensure you're not using `save()` without `update_fields`
- Check if the transition completed successfully
- Verify side effects didn't raise exceptions

**Solution**: Always use `update_fields=['state_field_name']` when manually saving state changes.

#### 3. Race Conditions
Multiple processes trying to transition the same object can cause race conditions.

**Solution**: Django-Logic serializes work on a state field with two mechanisms (see [Concurrency and locking](#concurrency-and-locking)):
- a **cache lock** (atomic set-if-absent on the `default` cache) held for a synchronous transition's whole flight and for a background transition's phase-1 critical section, with the persisted state re-validated under the lock; and
- the **`TransitionMessage` row** — while a background transition is in flight, a second one raises `AlreadyInProgress` and a synchronous transition on the same instance + process raises `TransitionNotAllowed`.

Use a cross-process cache (django-redis, installed automatically) so the lock is shared between web processes and workers. `RedisState` additionally caches the current state in the lock key for cross-process visibility, and works with background transitions:

```python
from django_logic.state import RedisState

class MyProcess(Process):
    state_class = RedisState
    # ... rest of configuration
```

#### 4. Side Effects Not Rolling Back
Side effects that modify external systems may not roll back automatically.

**Solution**: Implement compensating transactions using failure side-effects (run while locked) or failure callbacks (run after unlock):

```python
def compensate_payment(instance, exception, **kwargs):
    # Reverse the payment if side effect failed
    pass

Transition(
    action_name='pay',
    sources=['pending'],
    target='paid',
    side_effects=[process_payment, another_side_effect],
    failure_side_effects=[compensate_payment],  # runs before unlock (while instance is locked)
    failure_callbacks=[notify_admin],            # runs after unlock
)
```

When a side-effect fails, execution order is: set `failed_state` (if configured) → **failure_side_effects** → unlock → **failure_callbacks**. Use failure_side_effects for cleanup that must run before other processes can access the instance.

## Django-Logic vs Django FSM 
[Django FSM](https://github.com/viewflow/django-fsm) is a predecessor of Django-Logic. 
Django-Logic was created to address limitations and add new features:

### Key Differences:
- **Processes**: Django-Logic supports grouping transitions into processes
- **Nested Processes**: Build hierarchical workflows  
- **Built-in Locking**: Prevents race conditions out of the box
- **Failure Handling**: Dedicated failure side-effects, failure callbacks, and failed states
- **Better Separation**: Clear separation between business logic and implementation
- **Background Tasks**: Durable, queue-routed background execution built in via `django_logic.background` ([Background Transitions](#background-transitions)) — no external package required

### Migration from Django FSM:
If you're migrating from Django FSM, the main changes are:
1. Replace `@transition` decorator with `Transition` class
2. Move transition logic to side effects and callbacks
3. Group related transitions into Process classes
4. Bind each model to its process with `ProcessManager.bind_model_process(...)` in your app's `AppConfig.ready()` (see [Bind the process](#4-bind-the-process-in-your-apps-appconfigready))

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
            old_value=self.get_db_state(),
            new_value=state,
        )
        super().set_state(state)
```

### Context Passing
Pass data between side effects and callbacks:

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

For long-running side-effects (payment processing, PDF generation, external API calls), use `BackgroundTransition` / `BackgroundAction` from `django_logic.background`. **Background transitions are Celery tasks** — Celery ships as a core dependency and `'celery'` is the default execution mode.

**How execution is split (the "two phases").** A synchronous `Transition` does everything at once, in the caller's call frame. A background transition *cannot* — its work runs later, on another machine — so it follows the standard transactional-outbox pattern, and the docs/code refer to the two halves as:

- **Phase 1** (synchronous, in your request): validate, then in **one** database transaction write `in_progress_state` and a durable `TransitionMessage` row (the recorded intent), then enqueue the Celery task on commit. Fast — milliseconds.
- **Phase 2** (on a Celery worker): load the row, run the side-effects, write the target state, mark the row completed — all in one atomic block. If the worker crashes or the broker loses the message, the durable row from phase 1 is what lets the safety-net tasks retry or finalize the work. (Success/failure *callbacks* run after phase 2's transaction commits — best-effort by contract, sometimes called "phase 3" in the runner's comments; there is nothing beyond that.)

They provide:

- **Durable execution.** Every background transition is persisted as a `TransitionMessage` row inside the same atomic block that writes `in_progress_state`. Worker crashes, broker losses, and dropped `transaction.on_commit` hooks are all recovered by a periodic safety-net task.
- **Queue routing per transition.** `queue=` is optional — transitions without it run on `DJANGO_LOGIC['DEFAULT_QUEUE']` (`'django_logic'`). Name queues per SLA (`critical` / `slow` / `fast`) and give each its own worker to manage performance per queue.
- **Sync mode for tests.** `'sync'` runs phase 2 inline in the same process — for unit tests, CI, management commands, and the Django shell. No Celery broker is needed to test business processes; see [Testing Your Processes](#testing-your-processes).
- **Single-task, all-or-nothing attempts.** All side-effects plus the target-state write happen inside **one** Celery task with `acks_late=True`, inside **one** atomic block, with the side-effects in a savepoint: a failed attempt **rolls back every database write it made**. A worker crash re-delivers the whole task; the state never gets stuck mid-flight between side-effects. The idempotency you owe is for *external* calls only — a retried attempt re-runs side-effects from scratch.

### Install

Add `'django_logic.background'` to `INSTALLED_APPS` and configure:

```python
DJANGO_LOGIC = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'celery',   # the default; set 'sync' in test settings
    'DEFAULT_QUEUE': 'django_logic',    # queue for transitions without queue=
    'STARTER_QUEUE': 'django_logic.starter',
    'PHASE2_STATE_GUARD': 'enforce',    # see "The phase-2 state guard"
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}
```

Every key has the default shown above, so an empty `DJANGO_LOGIC = {}` is a valid production start. Run `manage.py migrate` to create the `TransitionMessage` table.

At boot, celery mode fails fast on two misconfigurations that would silently break the guarantees: a SQLite database for `TransitionMessage` (no `select_for_update(nowait)`), and — when `DEBUG=False` — a per-process `default` cache (locmem/dummy), because the state lock must be shared between web processes and workers:

```python
CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
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
# In a view — returns immediately (Celery mode) or after phase 2 completes (Sync mode).
tr_id = order.process.fulfil(user=request.user)
```

### Polymorphic routing with nested processes

Nested processes let several sub-processes share an `action_name` and be
selected at runtime by a **condition on the instance** — so a generic caller
invokes one method and the right implementation runs. This works for background
transitions too: each integration's durable work lives on its own nested
process, but callers never have to know which one.

```python
def is_gmail(conversation, **kw):  return conversation.source_integration == 'gmail'
def is_dummy(conversation, **kw):  return conversation.source_integration == 'dummy'

class GmailConversationProcess(Process):
    process_name = 'gmail_conversation'
    transitions = [
        BackgroundTransition(
            action_name='send_message_via_integration',
            sources=['open'], target='open',
            in_progress_state='gmail_sending',     # must be unique across the tree
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

Phase 1 resolves exactly one transition (the conditions are mutually exclusive)
and records the **owning nested process class** on the `TransitionMessage`;
phase 2 restores that exact transition from the recorded owner — it does not
re-evaluate the condition, so routing is deterministic even if the instance
changes mid-flight. Constraints: a background `action_name` must only be
**unique within a single process class** (two in one class are
indistinguishable at restore), and every `in_progress_state` must be unique
across the whole tree. A background `action_name` *may* coincide with a
synchronous transition of the same name (phase 2 restores only background
transitions; phase 1 routes the call by condition) — so a synchronous fast-path
and a durable background slow-path can share one `action_name`.

> **Upgrade note.** When you turn an existing, uniquely-named background
> transition into this shared-name nested pattern, deploy it with no in-flight
> rows for that action (or split it across two deploys). Rows enqueued by older
> code don't carry the owning-process discriminator; once the name becomes
> shared, phase 2 can't tell which nested sibling such a row meant and finalizes
> it without running its side-effects (safe, but the work won't run). Rows
> enqueued after the upgrade always record their owner.

### Testing your processes

Set `BACKGROUND_EXECUTION='sync'` in your test settings — the global default is `'celery'`, so this opt-in is required — and every `instance.process.fulfil(...)` call runs phase 1 **and** phase 2 inline, no broker involved:

```python
class FulfilmentTests(TestCase):
    def test_happy_path(self):
        order = Order.objects.create(status='approved')
        order.process.fulfil()
        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')

    def test_side_effect_failure_propagates(self):
        # NB: patch what the side-effect CALLS, not the side-effect itself —
        # the Transition captured the function object at class-definition
        # time, so patching its module attribute would not replace it.
        # (django_logic.testing's fail_side_effect= injection avoids this
        # footgun entirely.)
        order = Order.objects.create(status='approved')
        with patch('myapp.services.courier_client.book', side_effect=CourierError):
            with self.assertRaises(CourierError):
                order.process.fulfil()
```

If the global setting is `'celery'` but you need Sync mode for a specific block, use the context manager:

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
django_logic.starter    — the framework's periodic safety-net tasks
```

The periodic starter re-dispatches stale transitions back to their own queue — retried slow jobs never jump to the critical queue.

### Safety-net tasks

Four periodic tasks (run them on `STARTER_QUEUE` via Celery beat) keep the durable model self-healing:

- `retry_stale_transitions` — re-dispatches uncompleted rows older than `RETRY_MINUTES` (skipping rows whose current attempt is still within `RETRY_MINUTES`, so a live attempt isn't re-dispatched on every tick).
- `cleanup_completed_transitions` — deletes completed rows older than `CLEANUP_DAYS`.
- `detect_stuck_transitions` — finalizes rows stuck at `MAX_ERRORS` (writes `failed_state`, runs `failure_side_effects` **and** `failure_callbacks`, marks completed) so the retry loop stops.
- `watchdog_stale_attempts` — abandons attempts that exceeded their declared `timeout` (see below).

### Per-attempt timeouts

A `BackgroundTransition` (or `BackgroundAction`) may declare a per-attempt wall-clock budget with `timeout=<seconds>`:

```python
BackgroundTransition(
    action_name='generate_export',
    sources=['fulfilled'],
    target='exported',
    in_progress_state='exporting',
    failed_state='export_failed',
    queue='django_logic.slow',
    timeout=600,                       # abandon an attempt after 10 minutes
    side_effects=[build_csv, upload_to_s3],
)
```

`watchdog_stale_attempts` scans in-flight rows whose current attempt (`started_at`) has run past `timeout`, records a synthetic `TimeoutError` as a failed attempt, and — once `errors_count` reaches `MAX_ERRORS` — finalizes the row to `failed_state`. Rows without `timeout` are never watched. Because the watchdog cannot tell a crashed attempt from a merely slow one, a re-dispatched attempt may run side-effects again while the original is still executing — **side-effects must be idempotent against external systems** (their database writes are per-attempt atomic and roll back on failure, but an external API call made by both attempts happens twice).

### Concurrency and locking

Two mechanisms serialize work on a state field, each with a precise scope:

1. **The cache lock** (atomic set-if-absent on the `default` cache) is held for a *synchronous* transition's whole flight, and for a background transition's **phase-1 critical section only** (validate → create the `TransitionMessage` → write `in_progress_state`, then released). Both re-validate the **persisted** state under the lock before proceeding, so two requests racing to transition the same instance can't both win.
2. **The uncompleted `TransitionMessage` row** is the durable in-flight marker for background work. While one exists for an instance + process:
   - a second background transition raises `AlreadyInProgress` (`from django_logic.background.exceptions import AlreadyInProgress`) — enforced by a partial unique constraint, so it holds across processes and dynos;
   - a **synchronous transition on the same instance + process raises `TransitionNotAllowed`** — phase 2 owns the state field until the row completes;
   - synchronous `Action`s still run (they don't change state).

The constraint is scoped **per process**: two independent state machines bound to different fields of the same model (say `status` and `payment_status`) can both have background work in flight.

Because the in-flight marker is a database row rather than a held lock, nothing leaks if the caller's surrounding transaction rolls back — the row, the `in_progress_state` write, and the dispatch all disappear together.

Practical consequence: you **cannot** chain a background transition from another transition's `callbacks`/`next_transition` on the *same* instance while the first row is still uncompleted — the chained phase 1 will hit `AlreadyInProgress`. Chain follow-up background work from a *terminal* hook (success/failure callback that fires after the first row is marked completed), or target a different instance.

> ⚠️ **Swallow-dedup loses mid-execution updates.** Catching `AlreadyInProgress` as "already queued — the running job will pick up my changes" is only safe while the existing attempt has **not started**. If phase 2 is already executing, it has already read its inputs: your update lands after the read, the in-flight run commits a result computed from pre-update data, and nothing ever re-runs. For recompute-style transitions, persist a dirty flag (or version) *before* dispatching, clear it inside the side-effect, and re-dispatch from a success callback if it is set again:
>
> ```python
> def recompute(instance, **kwargs):
>     Order.objects.filter(pk=instance.pk).update(recompute_requested=False)
>     ...  # compute from current rows
>
> def redispatch_if_dirty(instance, **kwargs):   # success callback (terminal hook)
>     instance.refresh_from_db()
>     if instance.recompute_requested:
>         instance.process.recompute_rates()
> ```

### The phase-2 state guard

Phase 2 restores the transition by name and deliberately bypasses the source-state gate — so what happens if the instance was moved by something *else* while the row was pending (a manual ops fix in the admin, a data migration, a support script)? With retries spanning `RETRY_MINUTES × MAX_ERRORS`, that collision is a realistic production event.

Before running side-effects, phase 2 verifies the persisted state still matches what phase 1 left behind (`in_progress_state`, or a declared source when the transition has none). On mismatch:

- **`PHASE2_STATE_GUARD = 'enforce'`** (default) — the row is completed as **superseded**: side-effects are skipped, the external state change wins, and the reason is recorded on the row (`last_error_message` starts with `[superseded]`) and logged at ERROR.
- **`'warn'`** — log a warning and run anyway (pre-0.4 behaviour).

The same guard protects the `failed_state` writes made by the safety-net tasks, so a watchdog finalizing a long-stranded row never clobbers a manual fix.

### Production deployment

Celery mode has three things you **must** wire up, or the durability guarantees silently won't hold:

**1. A real broker.** `BACKGROUND_EXECUTION='celery'` requires a durable broker (Redis/RabbitMQ). With no broker configured, Celery falls back to an in-memory transport that no worker drains — `apply_async` succeeds but the task never runs (django-logic logs a one-time warning on first dispatch).

**2. The four periodic safety-net tasks, scheduled via Celery beat.** They are registered automatically (`@shared_task`, names `django_logic.*`) once your Celery app imports/auto-discovers `django_logic.background.tasks`. **If you don't schedule them, retries, stuck-row finalization, and the timeout watchdog never run** — a single lost broker message or crashed worker then strands an instance in `in_progress_state` forever.

Use the ready-made schedule — it routes all four tasks to `DJANGO_LOGIC['STARTER_QUEUE']` with the recommended intervals (retry 60s, detect-stuck 300s, watchdog 120s, cleanup daily), each overridable by keyword:

```python
# celery.py — after the app is configured
from django_logic.background import beat_schedule

app.conf.beat_schedule = {**app.conf.beat_schedule, **beat_schedule()}
```

(A hand-written `CELERY_BEAT_SCHEDULE` works exactly the same — the task names are `django_logic.retry_stale_transitions`, `django_logic.detect_stuck_transitions`, `django_logic.watchdog_stale_attempts`, `django_logic.cleanup_completed_transitions`; remember to set `options={'queue': ...}` per entry yourself.)

Run a worker that consumes both your transition queues **and** the starter queue, plus beat:

```bash
celery -A myproject worker -Q django_logic.critical,django_logic.slow,django_logic.fast,django_logic.starter
celery -A myproject beat        # (or `worker -B` in dev; use a single beat in prod)
```

**3. Crash re-delivery is built in.** Every django-logic task sets
`acks_late=True` **and** `reject_on_worker_lost=True` at the task level, so a
transition re-delivers if its worker dies mid-execution (SIGKILL / OOM /
deploy / `--max-memory-per-child` kills) regardless of your global Celery
configuration — nothing to wire up. Setting the global pair is still a good
idea for your *own* tasks:

```python
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
```

**Running behind pgbouncer (transaction pooling).** The concurrency guard
(`select_for_update(nowait)` + the partial-unique constraint) works under
pgbouncer **transaction** pooling, but transaction mode is incompatible with a
few PostgreSQL session features, so configure the consumer accordingly:

```python
DATABASES['default'].setdefault('OPTIONS', {})['prepare_threshold'] = None  # psycopg3: no server-side prepared stmts
DATABASES['default']['DISABLE_SERVER_SIDE_CURSORS'] = True
```

Also do **not** force `sslmode=require` on the app→pgbouncer connection (it's
local/plaintext; pgbouncer terminates TLS upstream). If you skip
`prepare_threshold=None`, phase 2 will intermittently fail/hang with
prepared-statement errors. (Validated end-to-end on Heroku behind an in-dyno
pgbouncer.)

**Monitoring.** In Celery mode a failed attempt is **logged** (`django-logic.transition` at ERROR) and recorded on the row, but is **not** re-raised as a Celery task exception (re-raising would spam alerts and risk `acks_late` redelivery for an already-resolved row). So watch the `TransitionMessage` table, not Celery task failures:

```sql
-- rows stuck at the error ceiling (detect_stuck should be finalizing these)
SELECT count(*) FROM django_logic_background_transitionmessage
 WHERE is_completed = false AND errors_count >= 5;            -- = TRANSITION_MESSAGE_MAX_ERRORS

-- attempts running far longer than expected (watchdog candidates)
SELECT count(*) FROM django_logic_background_transitionmessage
 WHERE is_completed = false AND started_at < now() - interval '15 minutes';

-- rows superseded by external state changes (worth an occasional review:
-- each one is a manual fix or external write that won over a pending transition)
SELECT count(*) FROM django_logic_background_transitionmessage
 WHERE last_error_message LIKE '[superseded]%';
```

Also alert on beat liveness — if beat stops, the safety net stops.

**Migrating an existing deployment.** Migration `0005` widens `instance_id` from integer to `varchar(255)` via `ALTER COLUMN ... TYPE` (Django emits the `USING ...::varchar` cast, so existing integer rows convert in place). On a very large `TransitionMessage` table this rewrites the column under a lock — run it in a maintenance window or with your usual online-migration tooling. Migration `0006` (0.4.0) adds the `field_name` column and swaps the partial unique constraint from per-instance (`dl_bg_only_one_uncompleted_per_instance`) to per-process (`dl_bg_one_uncompleted_per_process`) — a quick metadata + index change, safe to run in place.

## Testing Your Processes

FSM workflows are notoriously hard to test well — state transitions,
conditions, permissions, side-effects, background jobs, failures, retries, and
locking all interact. `django_logic.testing` gives you a **scenario-based** test
base class that reads like the business process itself and runs everything —
including background transitions — **inline, with no Celery broker**.

Two principles keep these tests worth writing (full rationale in
[docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md#journeys-not-mirrors)):

- **Test your process, not the machinery.** Delivery, retries and durability
  are the library's guarantees (its own regression + stability + Heroku
  suites), so your tests never need a broker.
- **Test the object's journey, not the wiring.** Assert what the object
  *became* — its state trajectory, the fields the side-effects changed, and
  what reaches the caller on failure — not merely that a hook you declared got
  called. A test that only checks "the side-effect ran" passes even when the
  side-effect does the wrong thing (and an AI regenerating the code regenerates
  that test too). Journey assertions fail when behaviour regresses.

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

        self.background_transition(order, 'fulfil')      # phase 1 + phase 2, no Celery
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

        self.assert_state(order, 'fulfilling')           # left in-progress
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
        self.assert_state(order, 'fulfilling')                  # left in-progress
        self.assert_raised(ConnectionError, match='down')
```

**Driving the process**

| Method | What it does |
|--------|--------------|
| `create_instance(**fields)` | Create a model instance (state via the `state_field` kwarg) |
| `transition(obj, action, **kwargs)` | Run a synchronous transition |
| `background_transition(obj, action, **kwargs)` | Run a `BackgroundTransition`/`BackgroundAction` phase 1 **and** phase 2 inline |
| `retry_transition(obj)` | Re-run the instance's uncompleted transition — simulates the periodic starter |

Add `fail_side_effect='name'`, `fail_with=SomeError(...)` to `background_transition`/`retry_transition`/`transition` to make a named side-effect raise. Only that side-effect is wrapped — every other one runs for real, so you exercise the true failure path. Add `expect_raises=SomeError` to assert the failure **propagated to the caller** (the `side_effects` re-raise contract), or `expect_raises=False` to assert it was **swallowed** (`callbacks` / `next_transition` / `failure_side_effects`); omit it to absorb the injected exception and assert on the recorded error instead.

**Assertions**

- *State & availability:* `assert_state` · `assert_state_trace` · `assert_available` / `assert_not_available` (optional `user=`).
- *Domain outcome* — what the object *became*: `capture` → `assert_changed` / `assert_unchanged` · `assert_related_count`.
- *Wiring* — that a hook ran (pair with an outcome assertion): `assert_side_effects_ran` / `assert_side_effects_not_ran` · `assert_callbacks_ran` · `assert_failure_side_effects_ran` / `assert_failure_callbacks_ran`.
- *Caller boundary & durable row:* `assert_raised` / `assert_not_raised` · `assert_error_recorded` · `assert_error_count` · `assert_transition_owner`.
- *The whole journey:* `assert_journey([JourneyStep(...)])`.

Side-effects and callbacks are **tracked, not mocked** (identified by function `__name__`) — the real code runs; the framework just records what executed. `assert_side_effects_ran` / `assert_callbacks_ran` are *wiring* checks (a hook ran, not that it did the right thing) — pair them with `assert_changed` / `assert_related_count` / `assert_state`.

**Snapshot & replay — turn a production bug into a test**

```python
from django_logic.testing import snapshot, from_snapshot

data = snapshot(order)          # JSON-able: fields, state, TransitionMessage, process status
```

Capture it from a Django shell, admin action, Sentry, or a log, then reproduce:

```python
class TestStuckOrder(ProcessScenario):
    process_class, model, state_field = OrderProcess, Order, 'status'

    def test_reproduce_and_fix(self):
        order = self.from_snapshot('fixtures/bug_12345.json')  # rebuilds instance + TransitionMessage
        self.assert_state(order, 'fulfilling')
        self.retry_transition(order)        # prove the fix
        self.assert_state(order, 'fulfilled')
```

**AI-readable failure output.** When an assertion fails, the error includes a numbered timeline of every step, the relevant `TransitionMessage`, and (with `snapshot_on_failure = True` on the class) a reproducible snapshot — so a person or an AI agent can see exactly where the process diverged without reading stack traces.

`ProcessScenario` extends `TransactionTestCase`, so it works with the durable `TransitionMessage` + atomic-block machinery. Full design: [docs/design/TESTING_SCENARIOS.md](docs/design/TESTING_SCENARIOS.md).

**The full guide — [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md)** — documents every test scenario for a process (happy paths, gating, failures, retries, terminal failures, one-in-flight conflicts, superseded rows, nested processes, snapshot replay) with copy-pasteable examples, and explains the philosophy: **you test your process; the library guarantees the background machinery** (validated by its own regression suite and a production-style Heroku matrix), so your tests never need a Celery broker.

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
