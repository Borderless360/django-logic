"""Transition-execution coverage (#132).

Static analysis of a consumer's test tree cannot see transitive execution
(a test drives a view/task which calls ``instance.process.action()``) or
dynamic dispatch (``getattr(process, action_name)()``). The engine can:
every initiation resolves ``(transition, owning_process)`` in one place and
notifies ``django_logic.process.transition_observers``.

This module records those notifications as ``(owning process class, action)``
pairs and diffs them against every transition declared by every bound
process (``ProcessManager.bindings``, nested processes included), so a test
run can answer "which transitions did the suite never drive?" exactly.

Two front-ends:

* :class:`TransitionCoverage` — in-memory context manager for
  single-process runs::

      with TransitionCoverage() as cov:
          ...  # run tests / drive processes
      report = cov.report()

* File-backed recording for parallel test runners (fork or spawn): set
  ``DJANGO_LOGIC['TRANSITION_COVERAGE_LOG'] = '/path/to/file.log'`` and every
  worker appends unique pairs (activated in ``AppConfig.ready``); afterwards
  ``coverage_report(log_path=...)`` merges and diffs.

Initiation semantics: a pair is recorded when a transition is resolved and
about to execute. Phase-2 background restore and retries do not re-notify —
phase 1 already recorded the pair.

No test-framework imports here: activation happens in ``AppConfig.ready``,
which also runs in production processes.
"""
from django_logic.process import ProcessManager, transition_observers


def _key(process_cls, action_name: str) -> str:
    return f'{process_cls.__module__}.{process_cls.__qualname__}\t{action_name}'


def iter_bound_transitions():
    """Yield ``(binding, owning_process_cls, transition)`` for every
    transition declared by every bound process, walking nested processes.

    A process class nested under several bindings is yielded once per
    binding — key on ``(owning class, action_name)`` to deduplicate.
    """
    for binding in ProcessManager.bindings:
        stack = [binding.process_class]
        seen = set()
        while stack:
            process_cls = stack.pop()
            if process_cls in seen:
                continue
            seen.add(process_cls)
            for transition in process_cls.transitions:
                yield binding, process_cls, transition
            stack.extend(process_cls.nested_processes)


def coverage_report(executed=None, log_path=None) -> dict:
    """Diff executed pairs against every bound transition.

    :param executed: iterable of recorder keys (see :func:`_key`), e.g.
        ``TransitionCoverage.executed``.
    :param log_path: path to a file-backed recording (merged with
        ``executed`` if both are given).
    :return: dict with ``total`` / ``executed`` / ``uncovered`` where
        ``uncovered`` is a sorted list of
        ``{'process': dotted_class, 'action': name, 'background': bool,
        'models': [model labels]}``.
    """
    executed_keys = set(executed or ())
    if log_path:
        with open(log_path) as fh:
            executed_keys.update(line.rstrip('\n') for line in fh if line.strip())

    declared = {}
    for binding, process_cls, transition in iter_bound_transitions():
        entry = declared.setdefault(_key(process_cls, transition.action_name), {
            'process': f'{process_cls.__module__}.{process_cls.__qualname__}',
            'action': transition.action_name,
            'background': bool(getattr(transition, 'is_background', False)),
            'models': set(),
        })
        entry['models'].add(binding.model._meta.label)

    uncovered = [
        {**entry, 'models': sorted(entry['models'])}
        for key, entry in sorted(declared.items())
        if key not in executed_keys
    ]
    return {
        'total': len(declared),
        'executed': len(declared) - len(uncovered),
        'uncovered': uncovered,
    }


class TransitionCoverage:
    """In-memory recorder; use as a context manager or ``start()``/``stop()``."""

    def __init__(self):
        self.executed = set()

    def _observe(self, owning_process_cls, action_name, instance):
        self.executed.add(_key(owning_process_cls, action_name))

    def start(self):
        if self._observe not in transition_observers:
            transition_observers.append(self._observe)
        return self

    def stop(self):
        if self._observe in transition_observers:
            transition_observers.remove(self._observe)

    def report(self) -> dict:
        return coverage_report(self.executed)

    __enter__ = start

    def __exit__(self, *exc_info):
        self.stop()


class _FileRecorder:
    """Appends each newly-seen pair to ``path``. Per-process dedup only:
    parallel workers may write duplicate lines — ``coverage_report`` merges
    via a set, so duplicates are harmless."""

    def __init__(self, path):
        self.path = path
        self.seen = set()

    def __call__(self, owning_process_cls, action_name, instance):
        key = _key(owning_process_cls, action_name)
        if key in self.seen:
            return
        self.seen.add(key)
        with open(self.path, 'a') as fh:
            fh.write(key + '\n')


_file_recorder = None


def start_file_recording(path) -> None:
    """Idempotently register a file-backed recorder (one per process).

    Called from ``AppConfig.ready`` when
    ``DJANGO_LOGIC['TRANSITION_COVERAGE_LOG']`` is set, so spawn-based
    parallel workers self-activate; fork-based workers inherit the parent's
    recorder."""
    global _file_recorder
    if _file_recorder is not None and _file_recorder.path == path:
        return
    stop_file_recording()
    _file_recorder = _FileRecorder(path)
    transition_observers.append(_file_recorder)


def stop_file_recording() -> None:
    global _file_recorder
    if _file_recorder is not None:
        if _file_recorder in transition_observers:
            transition_observers.remove(_file_recorder)
        _file_recorder = None
