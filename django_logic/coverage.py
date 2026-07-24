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

Initiation semantics: a pair is recorded when a transition is *resolved* —
an initiation refused later (lock contention, under-lock revalidation,
``AlreadyInProgress``) still counts as driven. Phase-2 background restore
and retries do not re-notify — phase 1 already recorded the pair.

One footgun worth knowing:

* The log file is append-only and never truncated — point each run at a
  fresh path (or delete the old file first), or stale pairs from earlier
  runs silently count as covered.

Declaration identity (#146): keys carry the declaration's kind
(sync/background) and shape (sources → target) besides the class and
action name, so condition-disambiguated same-name transitions — including
a sync + background namesake pair in one class — count and cover
separately. Two *literally identical* declarations still collapse (they
are behaviorally indistinguishable). Logs written by 0.8 recorders used
2-field ``class\taction`` keys; ``coverage_report`` still accepts them
with the old semantics (a legacy line covers every same-name namesake).

No test-framework imports here: activation happens in ``AppConfig.ready``,
which also runs in production processes.
"""
from django_logic.process import ProcessManager, transition_observers


def _pair(process_cls, action_name: str) -> str:
    """The 0.8-era 2-field prefix — still the anchor legacy log lines
    are matched against."""
    return f'{process_cls.__module__}.{process_cls.__qualname__}\t{action_name}'


def _key(process_cls, transition) -> str:
    """Stable per-declaration identity: class, action, kind, the declared
    shape, and a conditions fingerprint. Independent of declaration order
    (sources and condition names sorted), survives process restarts and
    transition-list reorders.

    The conditions fingerprint matters for the common polymorphic
    pattern: same-class namesakes that share sources→target and differ
    ONLY by conditions (per-courier variants) must not collapse. It uses
    the condition callables' qualnames — two anonymous lambdas can still
    collide, but named condition functions (the norm) stay distinct.
    """
    kind = 'bg' if getattr(transition, 'is_background', False) else 'sync'
    sources = '|'.join(sorted(transition.sources))
    target = transition.target or ''
    conditions = ','.join(sorted(
        getattr(fn, '__qualname__', None) or type(fn).__name__
        for fn in getattr(transition.conditions, 'commands', None) or ()
    ))
    return (f'{_pair(process_cls, transition.action_name)}'
            f'\t{kind}\t{sources}>{target}\t{conditions}')


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
        try:
            with open(log_path) as fh:
                executed_keys.update(
                    line.rstrip('\n') for line in fh if line.strip())
        except FileNotFoundError:
            # The recorder only creates the file on the first pair — a run
            # that drove no transitions is a valid (all-uncovered) report,
            # not a crash.
            pass

    # Legacy 0.8 recorders wrote 2-field 'class\taction' lines, which
    # cannot distinguish namesakes: they cover every declaration sharing
    # the prefix (the old semantics — no false "uncovered" churn on old
    # logs). Full keys cover exactly one declaration.
    legacy_pairs = {key for key in executed_keys if key.count('\t') == 1}

    declared = {}
    for binding, process_cls, transition in iter_bound_transitions():
        entry = declared.setdefault(_key(process_cls, transition), {
            'process': f'{process_cls.__module__}.{process_cls.__qualname__}',
            'action': transition.action_name,
            'background': bool(getattr(transition, 'is_background', False)),
            'sources': sorted(transition.sources),
            'target': transition.target or '',
            'models': set(),
            '_pair': _pair(process_cls, transition.action_name),
        })
        entry['models'].add(binding.model._meta.label)

    uncovered = [
        {k: v for k, v in {**entry, 'models': sorted(entry['models'])}.items()
         if k != '_pair'}
        for key, entry in sorted(declared.items())
        if key not in executed_keys and entry['_pair'] not in legacy_pairs
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

    def _observe(self, owning_process_cls, action_name, instance, transition):
        self.executed.add(_key(owning_process_cls, transition))

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

    def __call__(self, owning_process_cls, action_name, instance, transition):
        key = _key(owning_process_cls, transition)
        if key in self.seen:
            return
        with open(self.path, 'a') as fh:
            fh.write(key + '\n')
        # Marked seen only after the append succeeds — a transient write
        # failure (disk full, permissions) retries on the next initiation
        # instead of permanently dropping the pair.
        self.seen.add(key)


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
