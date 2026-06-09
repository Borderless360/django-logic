"""
Base test case and utilities for stability tests.

Provides:
  - StabilityTestCase: TransactionTestCase with Redis cleanup and leak detection
  - CrashSimulator: Injects crashes at specific points during side effect execution
  - WorkerCrashSimulated: Exception type used to simulate worker crashes
  - requires_real_redis: Skip decorator for tests that need real Redis (nx=True)
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
    """Raised to simulate a Celery worker crash (OOM, SIGKILL, deploy)."""
    pass


class CrashSimulator:
    """
    Wraps side effects to simulate worker crashes at specific points.

    Usage:
        sim = CrashSimulator(crash_during='call_courier')
        wrapped = [sim.wrap(fn) for fn in side_effects]
        # When call_courier is reached, WorkerCrashSimulated is raised.

    After a crash, `sim.calls` records which side effects actually ran.
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
    Tracks side effect execution counts to verify idempotency.

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
    Run `fn` concurrently in `n_threads` threads and collect results.

    Each thread gets its own database connection via Django's per-thread
    connection management. Returns a list of (result_or_None, exception_or_None)
    tuples.

    If args_per_thread is provided, it should be a list of (args, kwargs) tuples,
    one per thread. Otherwise fn is called with no arguments.
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
    Base test case for stability tests.

    - Uses TransactionTestCase for real transaction isolation
    - Clears Redis cache between tests to prevent lock leaks
    - Verifies no orphaned locks remain after each test (leak detection)
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
        """Verify that no Redis lock keys were leaked by the test."""
        for key in list(self._tracked_cache_keys):
            value = cache.get(key)
            if value is not None:
                self.fail(
                    f"Leaked lock detected: cache key '{key}' still has "
                    f"value '{value}' after test completed. This indicates "
                    f"a lock was never released."
                )

    def track_lock(self, state):
        """Register a state's cache key for leak detection in tearDown."""
        self._tracked_cache_keys.add(state._get_hash())

    def get_cache_value(self, state):
        """Read the raw cache value for a state's lock key."""
        return cache.get(state._get_hash())

    def assert_locked(self, state, msg=None):
        self.assertTrue(state.is_locked(), msg or f"Expected {state.instance_key} to be locked")

    def assert_unlocked(self, state, msg=None):
        self.assertFalse(state.is_locked(), msg or f"Expected {state.instance_key} to be unlocked")
