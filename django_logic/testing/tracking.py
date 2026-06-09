"""Side-effect / callback execution tracking and failure injection.

We do not mock. During a tracked transition we temporarily replace the
callables on the transition's hook bundles (``side_effects``, ``callbacks``,
``failure_side_effects``, ``failure_callbacks``) with thin wrappers that:

* record the hook's ``__name__`` *after* it runs successfully, and
* (for ``side_effects`` only) optionally raise a chosen exception instead of
  running a named hook — exercising the real failure path.

Transition objects are class-level and shared, and both the synchronous
``SideEffects.execute`` path and the background phase-2 runner iterate
``transition.side_effects.commands`` — so wrapping ``_commands`` instruments
both execution modes. Everything is restored on exit.
"""
from __future__ import annotations

from contextlib import contextmanager


_HOOK_SLOTS = (
    ('side_effects', 'side_effects_ran'),
    ('callbacks', 'callbacks_ran'),
    ('failure_side_effects', 'failure_side_effects_ran'),
    ('failure_callbacks', 'failure_callbacks_ran'),
)


class ExecutionTracker:
    """Records what ran during one tracked transition attempt."""

    def __init__(self):
        self.side_effects_ran: list[str] = []
        self.callbacks_ran: list[str] = []
        self.failure_side_effects_ran: list[str] = []
        self.failure_callbacks_ran: list[str] = []
        # The exception raised by an injected side-effect, if any.
        self.injected_exception: BaseException | None = None
        self.failed_side_effect: str | None = None


def _name(fn) -> str:
    return getattr(fn, '__name__', repr(fn))


def _coerce_exception(fail_with, hook_name) -> BaseException:
    if isinstance(fail_with, BaseException):
        return fail_with
    if isinstance(fail_with, type) and issubclass(fail_with, BaseException):
        return fail_with()
    if callable(fail_with):
        return fail_with()
    return Exception(f'injected failure in side-effect {hook_name!r}')


def _make_wrapper(fn, tracker, sink_attr, fail_side_effect, fail_with):
    name = _name(fn)
    inject = fail_side_effect is not None and name == fail_side_effect

    def wrapper(instance, **kwargs):
        if inject:
            exc = _coerce_exception(fail_with, name)
            tracker.injected_exception = exc
            tracker.failed_side_effect = name
            raise exc  # real hook never runs → it did NOT "run"
        result = fn(instance, **kwargs)
        getattr(tracker, sink_attr).append(name)  # record only after success
        return result

    # Preserve __name__ so django-logic's own logging (command.__name__) works.
    wrapper.__name__ = name
    return wrapper


@contextmanager
def track(transitions, *, fail_side_effect=None, fail_with=None):
    """Instrument the given transition objects for the duration of the block.

    ``transitions`` is the list of class-level ``Transition`` objects matching
    the action under test (usually one). Yields an :class:`ExecutionTracker`.
    Injection only targets ``side_effects`` hooks.
    """
    tracker = ExecutionTracker()
    saved: list[tuple] = []
    for transition in transitions:
        for slot, sink_attr in _HOOK_SLOTS:
            bundle = getattr(transition, slot, None)
            if bundle is None:
                continue
            original = bundle._commands
            saved.append((bundle, original))
            inject_here = fail_side_effect if slot == 'side_effects' else None
            bundle._commands = [
                _make_wrapper(fn, tracker, sink_attr, inject_here, fail_with)
                for fn in original
            ]
    try:
        yield tracker
    finally:
        for bundle, original in saved:
            bundle._commands = original
