.. django-logic documentation master file, created by
   sphinx-quickstart on Mon Dec 23 14:08:06 2019.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

Welcome to django-logic's documentation!
========================================

.. toctree::
   :maxdepth: 2
   :caption: Contents:



Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`

=============
Documentation
=============

The Django-Logic package provides a set of tools helping to build a reliable product within a limited time. Here is the functionality the package offers for you:

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
