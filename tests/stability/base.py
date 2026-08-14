"""Base test case and helpers for the stability suite.

  - StabilityTestCase: TransactionTestCase that clears the cache and fails a
    test which leaks a lock
  - CrashSimulator: raises at a chosen point inside a side effect
  - WorkerCrashSimulated: the exception it raises
  - requires_real_redis: skips a test that needs a real Redis (nx=True)
"""
import threading
import unittest

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.test import TransactionTestCase, tag


def _is_real_redis():
    backend = settings.CACHES.get('default', {}).get('BACKEND', '')
    return 'redis' in backend.lower()


def _is_postgres():
    engine = settings.DATABASES.get('default', {}).get('ENGINE', '')
    return 'postgresql' in engine


requires_real_redis = unittest.skipUnless(
    _is_real_redis(),
    "Requires a real Redis backend (LocMemCache does not support nx=True)"
)

requires_postgres = unittest.skipUnless(
    _is_postgres(),
    "Requires PostgreSQL (SQLite locks the entire DB under write contention)"
)


class WorkerCrashSimulated(Exception):
    """Stands in for a Celery worker that dies: out of memory, SIGKILL, or a
    deploy."""
    pass


class CrashSimulator:
    """
    Wraps side effects so a worker crash can happen at a chosen point.

    Usage:
        sim = CrashSimulator(crash_during='call_courier')
        wrapped = [sim.wrap(fn) for fn in side_effects]
        # Reaching call_courier raises WorkerCrashSimulated.

    After the crash, `sim.calls` lists the side effects that ran.
    """

    def __init__(self, crash_during=None, crash_after_nth_call=None):
        self.crash_during = crash_during
        self.crash_after_nth_call = crash_after_nth_call
        self.call_count = 0
        self.calls = []
        self._lock = threading.Lock()

    def wrap(self, side_effect):
        def wrapper(instance, **kwargs):
            with self._lock:
                self.call_count += 1
                count = self.call_count
            name = getattr(side_effect, '__name__', str(side_effect))
            if self.crash_during and name == self.crash_during:
                raise WorkerCrashSimulated(
                    f"Simulated worker crash during {name}"
                )
            if (self.crash_after_nth_call is not None
                    and count > self.crash_after_nth_call):
                raise WorkerCrashSimulated(
                    f"Simulated worker crash after call #{count}"
                )
            result = side_effect(instance, **kwargs)
            with self._lock:
                self.calls.append(name)
            return result
        wrapper.__name__ = getattr(side_effect, '__name__', 'wrapped')
        wrapper.__qualname__ = getattr(side_effect, '__qualname__', 'wrapped')
        return wrapper

    def reset(self):
        with self._lock:
            self.call_count = 0
            self.calls.clear()


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
        self.call_args = {}
        self._lock = threading.Lock()

    def track(self, fn):
        name = fn.__name__

        def wrapper(instance, **kwargs):
            result = fn(instance, **kwargs)
            with self._lock:
                self.counts[name] = self.counts.get(name, 0) + 1
                self.call_args.setdefault(name, []).append(
                    (instance.pk, kwargs.copy())
                )
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

    def setUp(self):
        super().setUp()
        cache.clear()
        self._tracked_cache_keys = set()

    def tearDown(self):
        self._assert_no_leaked_locks()
        cache.clear()
        super().tearDown()

    def _assert_no_leaked_locks(self):
        """Fail the test if it left a tracked lock key in the cache."""
        for key in list(self._tracked_cache_keys):
            value = cache.get(key)
            if value is not None:
                self.fail(
                    f"Leaked lock: cache key '{key}' still holds '{value}' "
                    f"after the test finished. A lock was never released."
                )

    def track_lock(self, state):
        """Register a state's cache key, so tearDown checks it for a leak."""
        self._tracked_cache_keys.add(state._get_hash())

    def get_cache_value(self, state):
        """Read the raw cache value for a state's lock key."""
        return cache.get(state._get_hash())

    def assert_locked(self, state, msg=None):
        self.assertTrue(state.is_locked(), msg or f"Expected {state.instance_key} to be locked")

    def assert_unlocked(self, state, msg=None):
        self.assertFalse(state.is_locked(), msg or f"Expected {state.instance_key} to be unlocked")
