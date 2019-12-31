# Django-Logic

[![Build Status](https://travis-ci.org/Borderless360/django-logic.svg?branch=master)](https://travis-ci.org/Borderless360/django-logic) [![Coverage Status](https://coveralls.io/repos/github/Borderless360/django-logic/badge.svg?branch=master)](https://coveralls.io/github/Borderless360/django-logic?branch=master)
     
Django Logic is a lightweight workflow framework aims to solve an open problem "Where to put the business logic in Django?".

Full documentation for the project is available at [wiki](https://github.com/Borderless360/django-logic/wiki)

 The Django-Logic package provides a set of tools helping to build a reliable product within a limited time. Here is the functionality the package offers for you:
- Implement state-based business processes combined into Processes. 
- Provides a business logic layer as a set of conditions, side-effects, permissions, and even celery-tasks combined into a transition class.
- In progress states 
- REST API actions - every transition could be turned into a POST request action within seconds by extending your ViewSet and Serialiser of Django-Rest-Framework  
- Background transitions via [django-logic-celery](https://github.com/Borderless360/django-logic-celery).
- Draw your business processes to get a full picture of all transitions and conditions. 
- Easy to read the business process 
- One and only one way to implement business logic. You will be able to extend and improve the Django-Logic functionality and available handlers. However, the business logic will remain the same and by following SOLID principles. 
- Test your business logic by unit-tests as pure functions. 
- Protects from overwritten states, locks, etc. already implemented in Django Logic and you could control the behaviour. 
- Several states can be combined under the same Model.

## Installation

Use the package manager [pip](https://pip.pypa.io/en/stable/) to install Django-Logic.

```bash
pip install django-logic
```

## Usage
1. Create a django project and start a new app
2. Create a new file `process.py` under the app and define your process.
```python
from django_logic import Process as BaseProcess, Transition


class Process(BaseProcess):
    states = (
        ('draft', 'Draft'),
        ('paid', 'Paid'),
        ('void', 'Void'),
    )

    transitions = [
        Transition(action_name='approve', sources=['draft'], target='approved'),
        Transition(action_name='void', sources=['draft', 'approved'], target='void'),
    ]
```
3. Display the process. It requires to install graphviz.
```bash
pip install graphviz
``` 

[![][invoice-img]][invoice-img]

4. Bind the process with a model 
```python
from django.db import models
from django_logic.process import ProcessManager
from .process import Process as InvoiceProcess


class Invoice(ProcessManager.bind_state_fields(status=InvoiceProcess), models.Model):
    status = models.CharField(choices=InvoiceProcess.states, default='draft', max_length=16, blank=True)
``` 
5. Usage
```python
invoice = Invoice.objects.create()
print(list([transition.action_name for transition in invoice.process.get_available_transitions())])
>> ['approve', 'void']
invoice.process.approve()
invoice.status
>> 'approved'

print(list([transition.action_name for transition in invoice.process.get_available_transitions())])
>> ['void']
invoice.process.void()
invoice.status
>> 'void'

```
## Contributing
Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

Please make sure to update tests as appropriate.

## License
[MIT](https://choosealicense.com/licenses/mit/)

## Project status
Under development


[invoice-img]: https://user-images.githubusercontent.com/6745569/71333209-2840f080-2574-11ea-84e6-633f20d7d78f.png