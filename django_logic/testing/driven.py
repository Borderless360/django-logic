"""Record which transitions a test suite actually drives.

Every drive through the process entrypoint passes
``Process._get_transition_method``, so wrapping it during a test block
records each action that ran — synchronous, action, or background enqueue.
The record diffs against a process's declarations to answer: which
declared transitions did this suite never drive?
"""
from __future__ import annotations

from contextlib import contextmanager

from django_logic.exceptions import TransitionNotAllowed


class DrivenTransitions:
    """What ran inside one ``record_driven_transitions()`` block."""

    def __init__(self):
        #: Action names that were driven. A drive counts when the
        #: transition ran, including one whose side-effect failed; a
        #: refusal (``TransitionNotAllowed``) does not count.
        self.action_names: set[str] = set()

    def undriven(self, process_class) -> list[str]:
        """Declared action names of ``process_class`` (its whole nested
        tree) that no recorded drive touched, sorted.

        Names are compared, not declarations: when nested processes share
        an action name, one drive covers the name.
        """
        from django_logic.process import _iter_process_tree

        declared = {
            transition.action_name
            for klass in _iter_process_tree(process_class)
            for transition in klass.transitions
        }
        return sorted(declared - self.action_names)


@contextmanager
def record_driven_transitions():
    """Record every transition driven inside the block.

    Yields a :class:`DrivenTransitions`. Wraps the one entrypoint every
    drive passes and restores it on exit, so nesting and parallel suites
    must not overlap a block.
    """
    from django_logic.process import Process

    record = DrivenTransitions()
    original = Process._get_transition_method

    def recording(self, action_name: str, **kwargs):
        try:
            result = original(self, action_name, **kwargs)
        except TransitionNotAllowed:
            # Refused before it ran — conditions, permissions, the busy
            # gate. Nothing was driven.
            raise
        except Exception:
            # The transition ran and its side-effect failed: driven.
            record.action_names.add(action_name)
            raise
        record.action_names.add(action_name)
        return result

    Process._get_transition_method = recording
    try:
        yield record
    finally:
        Process._get_transition_method = original
