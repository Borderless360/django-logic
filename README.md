# Django-Logic (under development)
[![Build Status](https://travis-ci.org/Borderless360/django-logic.svg?branch=master)](https://travis-ci.org/Borderless360/django-logic)

django-logic - easy way to implement state-based business logic 

Django Logic is a lightweight workflow framework that provides an additional layer to implement the business logic with a Django. Inspired by Django FSM package and UML-state machine, we have developed a package which tends to solve a problem “Where to put the business logic in Django”. 

The vision behind this package is that Django especially useful for small-medium size projects and developer teams. Especially who have many ideas needs to validate/business rules need to implement, validate and improve. The Django-Logic package provides a set of tools helping to build a reliable product within a limited time. Here is the functionality what the package can do for you:
- Implement state-based business processes combined into Processes. 
- Provides a business logic layer as a set of conditions, side-effects, permissions, and even celery-tasks combined into a transition class.
- In progress states 
- REST API actions - every transition could be turned into a POST request action within seconds by extending your ViewSet and Serialiser of Django-Rest-Framework 
- Form buttons (TODO) - add buttons to your form and templates based on available transitions 
- Background side-effects before or after transition executed. It gives you reach functionality to implement complicated business rules without into details.
- Draw your business processes to get a full picture of all transitions and conditions. 
- Easy to read the business process 
- One and only one way to implement business logic. You will be able to extend and improve the Django-Logic functionality and available handlers. However, the business logic will remain the same and by following SOLID principles. 
- Test your business logic by unit-tests as pure functions. 
- Protects from overwritten states, locks, etc. already implemented in Django Logic and you could control the behaviour. 
- Several states can be combined under the same Model.
