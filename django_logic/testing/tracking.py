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
        # What the caller ASKED to fail — so the scenario can detect an
        # injection that never fired (issue #94: a silent no-op would turn
        # a failure test into a happy-path run).
        self.requested_fail_side_effect: str | None = None
        # Ordered sequence of states written to the instance during this
        # drive (in_progress -> target, plus any next_transition follow-ups
        # and failed_state writes). Captured by wrapping State.set_state in
        # track(); lets a test assert HOW the object changed as the workflow
        # progressed, not just its final state.
        self.state_trace: list = []


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

    ``transitions`` is the list of class-level ``Transition`` objects to
    instrument — the scenario passes every transition reachable from the
    process class (issue #96: a single drive can execute more than the named
    action via ``next_transition`` and callback-triggered transitions, and
    those hooks must be visible to the assertions too). Yields an
    :class:`ExecutionTracker`. Injection only targets ``side_effects`` hooks.

    When ``fail_side_effect`` names a hook that exists on none of the
    instrumented transitions, ``ValueError`` is raised immediately — a typo
    or a renamed hook must not silently turn a failure test into a
    happy-path run (issue #94).
    """
    tracker = ExecutionTracker()
    tracker.requested_fail_side_effect = fail_side_effect

    if fail_side_effect is not None:
        known = {
            _name(fn)
            for transition in transitions
            for fn in getattr(getattr(transition, 'side_effects', None),
                              '_commands', None) or []
        }
        if fail_side_effect not in known:
            raise ValueError(
                f'fail_side_effect={fail_side_effect!r} does not match any '
                f'side-effect on the tracked transitions '
                f'(known side-effects: {sorted(known)}). Was the hook '
                f'renamed?'
            )

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

    # Record every persisted state write during the drive — the ordered
    # sequence of states the object passed through (in_progress -> target,
    # and any next_transition follow-ups, and failed_state on failure).
    # RedisState.set_state delegates to super(), so wrapping the base
    # State.set_state captures exactly one append per write for both. The
    # wrap is restored on exit alongside the hook bundles. Patching the
    # class method is safe because tests are single-threaded and the patch
    # is scoped to this contextmanager.
    from django_logic.state import State as _State
    original_set_state = _State.set_state

    def _recording_set_state(self, state):
        result = original_set_state(self, state)
        tracker.state_trace.append(state)
        return result
    _recording_set_state.__name__ = 'set_state'
    _State.set_state = _recording_set_state

    try:
        yield tracker
    finally:
        _State.set_state = original_set_state
        for bundle, original in saved:
            bundle._commands = original
