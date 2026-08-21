"""Durable background transitions for Django Logic.

Public API:

* :class:`BackgroundTransition` / :class:`BackgroundAction` — declarative
  background-executed transitions with per-transition queue routing.
* :func:`sync_execution` — context manager that forces the current block
  to run the worker path inline (for tests, management commands, the shell).
* :func:`retry_pending` — run the periodic safety-net task once inline.
* :func:`in_flight` — racy read of whether an uncompleted row is still
  being retried, for shaping "busy, try again shortly" answers at API
  seams.
* :class:`PermanentFailure` — raise from a side-effect to say the failure
  is permanent: the worker takes the terminal path instead of retrying.
* :func:`beat_schedule` — ready-made Celery beat entries for the four
  safety-net tasks, routed to ``DJANGO_LOGIC['STARTER_QUEUE']``.

All symbols are importable after Django's app registry is ready
(i.e. inside views, management commands, tests, signal handlers).
Attribute access is lazy so this package can be declared in
``INSTALLED_APPS`` without triggering model imports during
``apps.populate()``.
"""
from __future__ import annotations


_PUBLIC = {
    'BackgroundTransition': ('django_logic.background.transitions', 'BackgroundTransition'),
    'BackgroundAction': ('django_logic.background.transitions', 'BackgroundAction'),
    'sync_execution': ('django_logic.background.dispatch', 'sync_execution'),
    'retry_pending': ('django_logic.background.dispatch', 'retry_pending'),
    'in_flight': ('django_logic.background.dispatch', 'in_flight'),
    'PermanentFailure': ('django_logic.background.exceptions', 'PermanentFailure'),
    'beat_schedule': ('django_logic.background.settings', 'beat_schedule'),
}

__all__ = list(_PUBLIC.keys())


def __getattr__(name):
    if name not in _PUBLIC:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    import importlib
    module_path, attr = _PUBLIC[name]
    value = getattr(importlib.import_module(module_path), attr)
    globals()[name] = value
    return value
