"""Durable background transitions for Django Logic.

Public API:

* :class:`BackgroundTransition` — declarative background-executed
  transitions with per-transition queue routing. ``target=None``
  declares one that writes no state on success.
* :func:`sync_execution` — context manager that forces the current block
  to run the worker path inline (for tests, management commands, the shell).
* :func:`retry_pending` — run every claimable row inline, once.
* :func:`in_flight` — racy read of whether an uncompleted row is still
  being retried, for shaping "busy, try again shortly" answers at API
  seams.
* :class:`PermanentFailure` — raise from a side-effect to say the failure
  is permanent: the worker takes the terminal path instead of retrying.
* :func:`run_worker` — the pull worker loop (also exposed as the
  ``dl_worker`` management command).

All symbols are importable after Django's app registry is ready
(i.e. inside views, management commands, tests, signal handlers).
Attribute access is lazy so importing this package never triggers
model imports before the app registry is ready.
"""
from __future__ import annotations


_PUBLIC = {
    'BackgroundTransition': ('django_logic.background.transitions', 'BackgroundTransition'),
    'sync_execution': ('django_logic.conf', 'sync_execution'),
    'retry_pending': ('django_logic.background.safety_nets', 'retry_pending'),
    'in_flight': ('django_logic.background.models', 'in_flight'),
    'PermanentFailure': ('django_logic.background.exceptions', 'PermanentFailure'),
    'run_worker': ('django_logic.background.pull', 'run_worker'),
}

__all__ = list(_PUBLIC.keys())


def __getattr__(name):
    if name == 'BackgroundAction':
        raise ImportError(
            "BackgroundAction was removed in 1.0.0. Declare "
            "BackgroundTransition(action_name=..., sources=[...]) with no "
            "target — the behavior is identical: same durability, same "
            "lock and gate, no state write on success."
        )
    if name not in _PUBLIC:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    import importlib
    module_path, attr = _PUBLIC[name]
    value = getattr(importlib.import_module(module_path), attr)
    globals()[name] = value
    return value
