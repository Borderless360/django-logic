"""Durable background transitions for Django Logic.

Public API:

* :class:`BackgroundTransition` — declarative background-executed
  transitions with per-transition queue routing. ``target=None``
  declares one that writes no state on success.
* :func:`sync_execution` — context manager that forces the current block
  to run the worker path inline when business code or tests need the result immediately.
* :func:`retry_pending` — run every claimable row inline, once.
* :func:`in_flight` — racy read of whether an uncompleted row is still
  being retried, for shaping "busy, try again shortly" answers at API
  seams.
* :class:`PermanentFailure` — raise from a side-effect to say the failure
  is permanent: the worker takes the terminal path instead of retrying.
* :func:`transition_statistics` — read retained job counts and timings.
* :func:`queue_worker_presence` — read a queue's recent worker observation.

All symbols are importable after Django's app registry is ready
(i.e. inside views, management commands, tests, signal handlers).
Attribute access is lazy so importing this package never triggers
model imports before the app registry is ready.
"""
from __future__ import annotations


_PUBLIC = {
    'Defer': ('django_logic.background.exceptions', 'Defer'),
    'BackgroundTransition': ('django_logic.background.transitions', 'BackgroundTransition'),
    'sync_execution': ('django_logic.conf', 'sync_execution'),
    'retry_pending': ('django_logic.background.safety_nets', 'retry_pending'),
    'in_flight': ('django_logic.background.models', 'in_flight'),
    'PermanentFailure': ('django_logic.background.exceptions', 'PermanentFailure'),
    'transition_statistics': ('django_logic.background.diagnostics', 'transition_statistics'),
    'queue_worker_presence': ('django_logic.background.worker_presence', 'queue_worker_presence'),
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
