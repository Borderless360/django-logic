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

Declaration identity (#146, #153): keys carry the declaration's kind
(sync/background), shape (sources → target), and a conditions fingerprint
(the condition callables' sorted qualnames, plus the *configuration* of
partials and callable instances) besides the class and action name, so
condition-disambiguated same-name transitions — including a sync
+ background namesake pair, and per-courier variants differing only by
conditions — count and cover separately. Two *literally identical*
declarations still collapse (they are behaviorally indistinguishable).

No test-framework imports here: activation happens in ``AppConfig.ready``,
which also runs in production processes.
"""
import datetime
import decimal
import functools
import uuid

from django_logic.process import ProcessManager, transition_observers


# Types whose ``repr`` is derived from the value alone — safe to embed in a
# key that is compared across processes.
_STABLE_REPR_TYPES = (
    str, bytes, bool, int, float, complex, type(None),
    decimal.Decimal, uuid.UUID,
    datetime.datetime, datetime.date, datetime.time, datetime.timedelta,
)


def _stable_repr(value) -> str:
    """Render one piece of condition *configuration* process-independently.

    Keys are compared ACROSS processes — spawn-based workers append to one log
    and a separate process merges and diffs it — so a plain ``repr`` is
    unusable: ``<CourierIs object at 0x10f3a2b50>`` differs per process and per
    run, and every key carrying one would look uncovered forever. Values whose
    repr *is* the value are kept verbatim (that is what tells ``'ups'`` from
    ``'dhl'``); anything else degrades to its class path, which still separates
    declarations configured with different TYPES.
    """
    if isinstance(value, _STABLE_REPR_TYPES):
        return repr(value)
    if isinstance(value, (list, tuple)):
        inner = ','.join(_stable_repr(item) for item in value)
        return f'[{inner}]' if isinstance(value, list) else f'({inner})'
    if isinstance(value, (set, frozenset)):
        # Set iteration order varies with the (salted) string hash, i.e. per
        # process — sort the rendered items.
        return '{' + ','.join(sorted(_stable_repr(item) for item in value)) + '}'
    if isinstance(value, dict):
        return '{' + ','.join(sorted(
            f'{_stable_repr(key)}:{_stable_repr(item)}'
            for key, item in value.items())) + '}'
    qualname = getattr(value, '__qualname__', None)   # functions, classes
    if qualname:
        return f'{getattr(value, "__module__", "?")}.{qualname}'
    cls = type(value)
    return f'{cls.__module__}.{cls.__qualname__}'


def _instance_config(instance) -> str:
    """The stably-rendered attribute state of a callable condition instance.

    ``CourierIs('ups')`` and ``CourierIs('dhl')`` are two different
    declarations; without their state in the fingerprint they share one key and
    driving either marks both covered — a gate that greenlights a transition no
    test ever drove.
    """
    state = getattr(instance, '__dict__', None)
    if state is None:
        # ``__slots__`` classes have no ``__dict__``; read the declared slots
        # (walking the MRO) and fall back to the bare class path if there are
        # none to read.
        names = []
        for cls in type(instance).__mro__:
            slots = getattr(cls, '__slots__', ()) or ()
            names.extend([slots] if isinstance(slots, str) else list(slots))
        state = {name: getattr(instance, name)
                 for name in names if hasattr(instance, name)}
    return ','.join(f'{name}={_stable_repr(value)}'
                    for name, value in sorted(state.items()))


def _condition_fingerprint(fn) -> str:
    """A stable, distinguishing name for one condition callable.

    ``getattr(fn, '__qualname__', ...)`` alone degenerated exactly where the
    fingerprint matters most: ``functools.partial`` objects carry no
    ``__qualname__`` (so every partial fingerprinted as the literal
    ``'partial'``), and instances of callable condition classes do not inherit
    one either (``__qualname__`` is a descriptor on ``type``). Both are the
    idiomatic way to write the per-variant conditions this key exists to keep
    apart, so distinct declarations collapsed into one key and reported false
    coverage. Their *configuration* (bound args, instance attributes) is part
    of the fingerprint for the same reason, rendered via :func:`_stable_repr`
    so the key survives the cross-process merge.
    """
    qualname = getattr(fn, '__qualname__', None)
    if qualname:
        return qualname
    if isinstance(fn, functools.partial):
        inner = _condition_fingerprint(fn.func)
        bound = ','.join(
            [_stable_repr(a) for a in fn.args or ()]
            + [f'{k}={_stable_repr(v)}'
               for k, v in sorted((fn.keywords or {}).items())]
        )
        return f'partial({inner}({bound}))'
    cls = type(fn)
    path = f'{cls.__module__}.{cls.__qualname__}'
    config = _instance_config(fn)
    return f'{path}({config})' if config else path


def _key(process_cls, transition) -> str:
    """Stable per-declaration identity: class, action, kind, the declared
    shape, and a conditions fingerprint. Independent of declaration order
    (sources and condition names sorted), survives process restarts and
    transition-list reorders.

    The conditions fingerprint matters for the common polymorphic
    pattern: same-class namesakes that share sources→target and differ
    ONLY by conditions (per-courier variants) must not collapse. It uses
    the condition callables' qualnames — two anonymous lambdas can still
    collide, but named condition functions (the norm) stay distinct, and so
    do ``functools.partial`` and callable-instance conditions *including their
    per-variant configuration* (see ``_condition_fingerprint``).
    """
    kind = 'bg' if getattr(transition, 'is_background', False) else 'sync'
    sources = '|'.join(sorted(transition.sources))
    target = transition.target or ''
    conditions = ','.join(sorted(
        _condition_fingerprint(fn)
        for fn in getattr(transition.conditions, 'commands', None) or ()
    ))
    return (f'{process_cls.__module__}.{process_cls.__qualname__}'
            f'\t{transition.action_name}'
            f'\t{kind}\t{sources}>{target}\t{conditions}')


def iter_bound_transitions():
    """Yield ``(binding, owning_process_cls, transition)`` for every
    transition declared by every bound process, walking nested processes.

    A process class nested under several bindings is yielded once per
    binding — key on ``(owning class, action_name)`` to deduplicate.
    """
    from django_logic.process import _iter_process_tree

    for binding in ProcessManager.bindings:
        for process_cls in _iter_process_tree(binding.process_class):
            for transition in process_cls.transitions or []:
                yield binding, process_cls, transition


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

    declared = {}
    for binding, process_cls, transition in iter_bound_transitions():
        entry = declared.setdefault(_key(process_cls, transition), {
            'process': f'{process_cls.__module__}.{process_cls.__qualname__}',
            'action': transition.action_name,
            'background': bool(getattr(transition, 'is_background', False)),
            'sources': sorted(transition.sources),
            'target': transition.target or '',
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
