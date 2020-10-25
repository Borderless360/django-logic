![django-logic](https://user-images.githubusercontent.com/6745569/87846635-dabb1500-c903-11ea-9fae-f1960dd2f82d.png)

[![Build Status](https://travis-ci.org/Borderless360/django-logic.svg?branch=master)](https://travis-ci.org/Borderless360/django-logic) 
[![Coverage Status](https://coveralls.io/repos/github/Borderless360/django-logic/badge.svg?branch=master)](https://coveralls.io/github/Borderless360/django-logic?branch=master)
     
Django Logic is a workflow framework allowing developers to implement business logic via pure functions. 
It's designed based on [Finite-State-Machine (FSM)](https://en.wikipedia.org/wiki/Finite-state_machine) principles. 
Therefore, it needs to define a `state` field for a model's object. Every change of the `state` is performed by a 
transition and every transition could be grouped into a process. Also, you can define some side-effects that will be
executed during the transition from one state to another and callbacks that will be run after. 
This concept provides you a place for the business logic, rather than splitting it across the views, models, forms, 
serializers or even worse, in templates. 

## Definitions 
- **Transition** class changes a state of an object from one to another. It also contains its own conditions,
 permissions, side-effects, callbacks, and failure callbacks. 
- **Side-Effects** class defines a set of _functions_ that executing within one particular transition
 before reaching the `target` state. During the execution, the state changes to the `in_progress` state.
 In case, if one of the functions interrupts the execution, then it changes to the `failed` state.
- **Callbacks** class defines a set of _idempotent functions_ that executing within one particular transition
 after reaching the `target` state. In case, if one of the functions interrupts the execution, it will log
  an exception and the execution will be stopped (without changing the state to failed). 
- **Failure callbakcs** class defines a set of _idempotent functions_ that executing within one particular 
transition in case if one of the side-effects has been failed to execute. 
- **Conditions** class defines a set of _pure functions_ which receives an object
 and return `True` or `False` based on 
one particular requirement.
- **Permissions** class defines a set of _pure functions_ which receives an object and user, then returns `True` or 
`False` based on given permissions.
- **Process** class defines a set of transitions with some common conditions and permissions.
- **Nested Process** class defines a set of processes with some common conditions and permissions.

## Installation

Use the package manager [pip](https://pip.pypa.io/en/stable/) to install Django-Logic.

```bash
pip install django-logic
```

## Usage
1. Define a process class with some transitions.
```python
from django_logic import Process as BaseProcess, Transition, ProcessManager


class Process(BaseProcess):
    states = (
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('void', 'Void'),
    )
    transitions = [
        Transition(action_name='approve', sources=['draft'], target='approved'),
        Transition(action_name='void', sources=['draft', 'approved'], target='void'),
    ]

ApprovalProcess = ProcessManager.bind_state_fields(status=Process)
```

2. Bind the process with a model 
```python
from django.db import models
from .process import ApprovalProcess

class Invoice(ApprovalProcess, models.Model):
    status = models.CharField(choices=ApprovalProcess.states, default='draft', max_length=16, blank=True)
``` 

3. Advance your process with conditions, side-effects, and callbacks into the process
```python 
class Process(BaseProcess):
    permissions = [
        is_accountant, 
    ]
    states = (
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('void', 'Void'),
    )
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
            ]
        ),
        Transition(
            action_name='void', 
            callbakcs=[
                send_void_invoice_email_to_accountant
            ],
            sources=['approved'],
            target='void'
        ),
    ]
```

4. This approval process defines the business logic where:
- The user who performs the action must have accountant role (permission).
- It shouldn't be possible to invoice inactive customers (condition). 
- Once the invoice record is approved, it should generate a PDF file and send it to 
an accountant via email. (side-effects  and callbacks)
- If the invoice voided it needs to notify the accountant about that.
As you see, these business requirements should not know about each other. Furthermore, it gives a simple way 
to test every function separately as Django-Logic takes care of connection them into the business process.  

5. Execute in the code:
```python
from invoices.models import Invoice


def approve_view(request, pk):
    invoice = Invoice.objects.get(pk=pk)
    invoice.process.approve(user=request.user)
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

## Contributing
Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

Please make sure to update tests as appropriate.

## License
[MIT](https://choosealicense.com/licenses/mit/)

## Project status
Under development


[diagram-img]: https://user-images.githubusercontent.com/6745569/74101382-25c24680-4b74-11ea-8767-0eabd4f27ebc.png
