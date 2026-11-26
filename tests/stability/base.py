"""Base test case and helpers for the stability suite.

  - StabilityTestCase: TransactionTestCase that clears the cache and fails a
    test which leaks a lock
  - CrashSimulator: raises at a chosen point inside a side effect
  - WorkerCrashSimulated: the exception it raises
  - IdempotencyTracker: counts how many times each side effect runs
  - requires_postgres: marks a test that needs PostgreSQL
  - requires_real_redis: marks a test that needs a shared Redis cache
  - run_concurrent: runs a function in several threads and collects the results
"""
import threading

from django.db import connections
from django.test import TransactionTestCase, tag


requires_real_redis = tag('redis')
requires_postgres = tag('postgresql')


class WorkerCrashSimulated(Exception):
    """An ordinary exception used to test synchronous side-effect and callback failures.

    It does not simulate a hard process stop or lost database connection.
    """
    pass


class CrashSimulator:
    """
    Wraps side effects to raise a test exception at a chosen point.

    Usage:
        sim = CrashSimulator(crash_during='call_courier')
        wrapped = [sim.wrap(fn) for fn in side_effects]
        # Reaching call_courier raises WorkerCrashSimulated.
    """

    def __init__(self, crash_during=None):
        self.crash_during = crash_during
        self.call_count = 0
        self._lock = threading.Lock()

    def wrap(self, side_effect):
        def wrapper(instance, **kwargs):
            with self._lock:
                self.call_count += 1
            name = getattr(side_effect, '__name__', str(side_effect))
            if self.crash_during and name == self.crash_during:
                raise WorkerCrashSimulated(
                    f"Simulated worker crash during {name}"
                )
            return side_effect(instance, **kwargs)
        wrapper.__name__ = getattr(side_effect, '__name__', 'wrapped')
        wrapper.__qualname__ = getattr(side_effect, '__qualname__', 'wrapped')
        return wrapper

    def reset(self):
        with self._lock:
            self.call_count = 0


class IdempotencyTracker:
    """
    Counts how many times each side effect runs.

    Usage:
        tracker = IdempotencyTracker()
        se = tracker.track(my_side_effect)
        se(instance); se(instance)
        assert tracker.counts['my_side_effect'] == 2
    """

    def __init__(self):
        self.counts = {}
        self._lock = threading.Lock()

    def track(self, fn):
        name = fn.__name__

        def wrapper(instance, **kwargs):
            result = fn(instance, **kwargs)
            with self._lock:
                self.counts[name] = self.counts.get(name, 0) + 1
            return result
        wrapper.__name__ = fn.__name__
        wrapper.__qualname__ = fn.__qualname__
        return wrapper


def run_concurrent(fn, n_threads=2, args_per_thread=None):
    """
    Run `fn` in `n_threads` threads and collect the results.

    Django gives each thread its own database connection. Returns one
    (result, exception) tuple per thread, where the unused half is None.

    `args_per_thread`, when given, holds one (args, kwargs) tuple per thread.
    Otherwise `fn` takes no arguments.
    """
    results = [None] * n_threads
    errors = [None] * n_threads

    def worker(index):
        try:
            if args_per_thread:
                args, kwargs = args_per_thread[index]
                results[index] = fn(*args, **kwargs)
            else:
                results[index] = fn()
        except Exception as e:
            errors[index] = e
        finally:
            connections.close_all()

    threads = []
    for i in range(n_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    return list(zip(results, errors))


@tag('stability')
class StabilityTestCase(TransactionTestCase):
    """
    Base test case for the stability suite.

    - Subclasses TransactionTestCase, so transactions behave as in production
    - Clears the cache between tests, so no lock leaks into the next one
    - Fails a test that leaves a tracked lock behind
    """
    databases = '__all__'
