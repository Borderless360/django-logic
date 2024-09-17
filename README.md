![django-logic](https://user-images.githubusercontent.com/6745569/87846635-dabb1500-c903-11ea-9fae-f1960dd2f82d.png)

[![Build Status](https://travis-ci.org/Borderless360/django-logic.svg?branch=master)](https://travis-ci.org/Borderless360/django-logic) 
[![Coverage Status](https://coveralls.io/repos/github/Borderless360/django-logic/badge.svg?branch=master)](https://coveralls.io/github/Borderless360/django-logic?branch=master)
     
Django Logic is a workflow framework allowing developers to implement the business logic via pure functions. 
It's designed based on [Finite-State-Machine (FSM)](https://en.wikipedia.org/wiki/Finite-state_machine) principles. 
Therefore, it needs to define a `state` field for a model's object. Every change of the `state` is performed by a 
transition and every transition could be grouped into a process. Also, you can define some side-effects that will be
executed during the transition from one state to another and callbacks that will be run after. 
This concept provides you a place for the business logic, rather than splitting it across the views, models, forms, 
serializers or even worse, in templates. 

## Definitions 
- **Transition** - class changes a state of an object from one to another. It also contains its own conditions,
 permissions, side-effects, callbacks, and failure callbacks. 
- **Action** - in contrast with the transition, the action does not change the state. 
But it contains its own conditions, permissions, side-effects, callbacks, and failure callbacks. 
- **Side-effects** - class defines a set of functions that executing within one particular transition
 before reaching the `target` state. During the execution, the state changes to the `in_progress` state.
 In case, if one of the functions interrupts the execution, then it changes to the `failed` state.
- **Callbacks** - class defines a set of functions that executing within one particular transition
 after reaching the `target` state. In case, if one of the functions interrupts the execution, it will log
 an exception and the execution will be stopped (without changing the state to failed). 
- **Failure callbacks** - class defines a set of functions that executing within one particular 
transition in case if one of the side-effects has been failed to execute. 
- **Conditions** - class defines a set of functions which receives an object
 and return `True` or `False` based on one particular requirement.
- **Permissions** - class defines a set of functions which receives an object and user, then returns `True` or 
`False` based on given permissions.
- **Process** - class defines a set of transitions with some common conditions and permissions.
It also accepts nested processes that allow building the hierarchy.

## Installation

Use the package manager [pip](https://pip.pypa.io/en/stable/) to install Django-Logic.

```bash
pip install django-logic
```

## Usage
0. Add to INSTALLED_APPS
```python
INSTALLED_APPS = (
    ...
    'django_logic',
    ...
)
```

1. Define django model with one or more state fields. 
```python
from django.db import models


MY_STATE_CHOICES = (
     ('draft', 'Draft'),
     ('approved', 'Approved'),
     ('paid', 'Paid'),
     ('void', 'Void'),
 )

class Invoice(models.Model):
    my_state = models.CharField(choices=MY_STATE_CHOICES, default='open', max_length=16, blank=True)    
    my_status = models.CharField(choices=MY_STATE_CHOICES, default='draft', max_length=16, blank=True)
    
```

2. Define a process class with some transitions.
```python
from django_logic import Process as BaseProcess, Transition, Action
from .choices import MY_STATE_CHOICES


class MyProcess(BaseProcess):
    states = MY_STATE_CHOICES
    transitions = [
        Transition(action_name='approve', sources=['draft'], target='approved'),
        Transition(action_name='pay', sources=['approve'], target='paid'),
        Transition(action_name='void', sources=['draft', 'approved'], target='void'),
        Action(action_name='update', side_effects=[update_data]),
    ]
```

3. Bind the process with a model.
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

4. Advance your process with conditions, side-effects, and callbacks into the process. Use next_transition to automatically continue the process. 
```python 
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
            ]
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

5. This approval process defines the business logic where:
- The user who performs the action must have accountant role (permission).
- It shouldn't be possible to invoice inactive customers (condition). 
- Once the invoice record is approved, it should generate a PDF file and send it to 
an accountant via email. (side-effects  and callbacks)
- If the invoice voided it needs to notify the accountant about that.
As you see, these business requirements should not know about each other. Furthermore, it gives a simple way 
to test every function separately as Django-Logic takes care of connection them into the business process.  

6. Execute in the code:
```python
from invoices.models import Invoice


def approve_view(request, pk):
    invoice = Invoice.objects.get(pk=pk)
    invoice.my_process.approve(user=request.user, context={'my_var': 1})
```
Use context to pass data between side-effects and callbacks.

7. If you want to override the value of the state field, it must be done explicitly. For example: 
```python
Invoice.objects.filter(status='draft').update(my_state='open')
# or 
invoice = Invoice.objects.get(pk=pk)
invoice.my_state = 'open'
invoice.save(update_fields=['my_state'])
```
Save without `update_fields` won't update the value of the state field in order to protect the data from corrupting. 

8. Error handling:
```python 
from django_logic.exceptions import TransitionNotAllowed

try:
    invoice.my_process.approve()
except TransitionNotAllowed:
    logger.error('Approve is not allowed') 
```

#### Display process
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

Drawing such diagram requires installing graphviz.
```bash
pip install graphviz
``` 
Run this command
```python
from django_logic.display import * 
from demo.process import LockerProcess
display_process(LockerProcess, state='open', skip_main_process=True)
```

## Django-Logic vs Django FSM 
[Django FSM](https://github.com/viewflow/django-fsm) is a parent package of Django-Logic. 
It's been used in production for many years until the number of new ideas and requirements swamped us.
Therefore, it's been decided to implement these ideas under a new package. For example, supporting Processes or 
background transitions which were implemented under [Django-Logic-Celery](https://github.com/Borderless360/django-logic-celery).
Finally, we want to provide a standard way on where to put the business logic in Django by using Django-Logic. 

## Contributing
Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

Please make sure to update tests as appropriate.

## License
[MIT](https://choosealicense.com/licenses/mit/)

## Project status
Under development


[diagram-img]: https://user-images.githubusercontent.com/6745569/74101382-25c24680-4b74-11ea-8767-0eabd4f27ebc.png
