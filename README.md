![django-logic](https://user-images.githubusercontent.com/6745569/87846635-dabb1500-c903-11ea-9fae-f1960dd2f82d.png)

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

## Definitions 
- **Transition** class changes a state of an object from one to another. It also contains its own conditions,
 permissions, side-effects, callbacks, and failure callbacks. 
- **Side-Effects** class defines a set of _idempotent functions_ that executing within one particular transition
 before reaching the `target` state. During the execution, the state changes to the `in_progress` state.
 In case, if one of the functions interrupts the execution, then it changes to the `failed` state.
- **Callbacks** class defines a set of _idempotent functions_ that executiing within one particular transition
 after reaching the `target` state. In case, if one of the functions interrupts the execution, it will log
  an exception and the execution will be stopped (without changing the state to failed). 
- **Failure callbacks** class defines a set of _idempotent functions_ that executing within one particular 
transition in case if one of the side-effects has been failed to execute. 
- **Conditions** class defines a set of _pure functions_ which receives an object and return `True` or `False` based on 
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
#### Transition
#### Process
```python
from django_logic import Process, Transition

class ApprovalProcess(Process):
    transitions = [
        Transition(action_name='approve', sources=['draft'], target='approved'),
        Transition(action_name='void', sources=['draft', 'approved'], target='void'),
    ]
```
#### Nested processes 


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

Bind the process with a model 
```python
from django.db import models
from demo.process import LockerProcess
from django_logic.process import ProcessManager


class Lock(ProcessManager.bind_state_fields(status=LockerProcess), models.Model):
    status = models.CharField(choices=LockerProcess.states, default=LockerProcess.states.open, max_length=16, blank=True)
``` 

## Contributing
Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

Please make sure to update tests as appropriate.

## License
[MIT](https://choosealicense.com/licenses/mit/)

## Project status
Under development


[diagram-img]: https://user-images.githubusercontent.com/6745569/74101382-25c24680-4b74-11ea-8767-0eabd4f27ebc.png
