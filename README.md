![django-logic](https://user-images.githubusercontent.com/6745569/87846635-dabb1500-c903-11ea-9fae-f1960dd2f82d.png)

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
- [Display Process](#display-process)
- [Django-Logic vs Django FSM](#django-logic-vs-django-fsm)
- [Contributing](#contributing)
- [License](#license)

## Features
- 🎯 **Clear Business Logic** - Separate business logic from views, models, and forms
- 🔒 **Built-in Permissions** - Define who can perform which transitions
- 🔄 **Side Effects** - Execute functions during state transitions
- 📊 **Process Visualization** - Visualize your workflows
- 🏗️ **Nested Processes** - Build complex workflows with sub-processes
- ⚡ **Optimistic Locking** - Prevent race conditions
- 🔍 **Comprehensive Logging** - Track all state changes

## Requirements
- Python 3.6+
- Django 2.0+
- django-model-utils 4.5.1

## Installation

Use pip to install Django-Logic:

```bash
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
from django_logic import Process, Transition, ProcessManager

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

ProcessManager.bind_model_process(Order, OrderProcess, state_field='status')

# Usage
order = Order.objects.create()
order.process.pay()  # Changes status from 'pending' to 'paid'
```

## Core Concepts

### Definitions 
- **Transition** - Changes the state of an object from one to another. Contains conditions, permissions, side-effects, callbacks, and failure callbacks.
- **Action** - Similar to transition but doesn't change the state. Useful for operations that need permissions and side effects without state change.
- **Side-effects** - Functions executed during a transition before reaching the target state. If any fail, the transition is rolled back.
- **Callbacks** - Functions executed after successfully reaching the target state.
- **Failure callbacks** - Functions executed if side-effects fail.
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
    states = MY_STATE_CHOICES
    transitions = [
        Transition(action_name='approve', sources=['draft'], target='approved'),
        Transition(action_name='pay', sources=['approved'], target='paid'),
        Transition(action_name='void', sources=['draft', 'approved'], target='void'),
        Action(action_name='update', side_effects=[update_data]),
    ]
```

### 4. Bind the process with a model
```python
from django_logic import Process as BaseProcess, Transition, ProcessManager, Action
from .models import Invoice, MY_STATE_CHOICES


class MyProcess(BaseProcess):
    states = MY_STATE_CHOICES
    transitions = [
        Transition(action_name='approve', sources=['draft'], target='approved'),
        Transition(action_name='void', sources=['draft', 'approved'], target='void'),
        Action(action_name='update', side_effects=[update_data]),
    ]

ProcessManager.bind_model_process(Invoice, MyProcess, state_field='my_state')
``` 

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
    states = MY_STATE_CHOICES
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

### 8. Handle state field overrides
If you want to override the value of the state field, it must be done explicitly. For example: 
```python
Invoice.objects.filter(my_state='draft').update(my_state='approved')
# or 
invoice = Invoice.objects.get(pk=pk)
invoice.my_state = 'approved'
invoice.save(update_fields=['my_state'])
```
Save without `update_fields` won't update the value of the state field in order to protect the data from corrupting. 

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
from django_logic import Process, Transition, ProcessManager

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

## Display Process
Drawing a process with the following elements:
- Process - a transparent rectangle 
- Transition - a grey rectangle 
- State - a transparent ellipse 
- Process' conditions and permissions are defined inside of related process as a transparent diamond
- Transition' conditions and permissions are defined inside of related transition's process as a grey diamond
   
[![][diagram-img]][diagram-img]

From this diagram you can visually check that the following the business requirements have been implemented properly:
- Personnel involved: User and Staff
- Lock has to be available before any actions taken. It's  defined by a condition  `is_lock_available`. 
- User is able to lock and unlock an available locker. 
- Staff is able to lock, unlock and put a locker under maintenance if such was planned.  

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

**Solution**: Django-Logic uses optimistic locking by default. For critical operations, consider using `RedisState` for better distributed locking.

```python
from django_logic.state import RedisState

class MyProcess(Process):
    state_class = RedisState
    # ... rest of configuration
```

#### 4. Side Effects Not Rolling Back
Side effects that modify external systems may not roll back automatically.

**Solution**: Implement compensating transactions in failure callbacks:

```python
def compensate_payment(instance, exception, **kwargs):
    # Reverse the payment if side effect failed
    pass

Transition(
    action_name='pay',
    sources=['pending'],
    target='paid',
    side_effects=[process_payment, another_side_effect],
    failure_callbacks=[compensate_payment],
)
```

## Django-Logic vs Django FSM 
[Django FSM](https://github.com/viewflow/django-fsm) is a predecessor of Django-Logic. 
Django-Logic was created to address limitations and add new features:

### Key Differences:
- **Processes**: Django-Logic supports grouping transitions into processes
- **Nested Processes**: Build hierarchical workflows  
- **Built-in Locking**: Prevents race conditions out of the box
- **Failure Handling**: Dedicated failure callbacks and states
- **Better Separation**: Clear separation between business logic and implementation
- **Background Tasks**: Celery integration via [Django-Logic-Celery](https://github.com/Borderless360/django-logic-celery)

### Migration from Django FSM:
If you're migrating from Django FSM, the main changes are:
1. Replace `@transition` decorator with `Transition` class
2. Move transition logic to side effects and callbacks
3. Group related transitions into Process classes
4. Update state field management to use ProcessManager

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
            instance_id=self.instance.id,
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

## Contributing
Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

### Development Setup
1. Clone the repository
2. Create a virtual environment: `python -m venv venv`
3. Install dependencies: `pip install -r requirements.txt`
4. Run tests: `python tests/manage.py test`

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


[diagram-img]: https://user-images.githubusercontent.com/6745569/74101382-25c24680-4b74-11ea-8767-0eabd4f27ebc.png
