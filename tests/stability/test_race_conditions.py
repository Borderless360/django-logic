"""
Category 2: Race Condition Tests

Proves that concurrent requests for the same instance are handled safely:
exactly one wins, the other is rejected. No duplicate state changes,
no duplicate side-effect execution.

The core mechanism is:
  - RedisState: immediate lock visibility across processes/threads
  - DB UniqueConstraint on TransitionMessage: prevents duplicate work records
  - select_for_update(nowait=True): prevents two workers from running
    the same message concurrently

Reference: docs/research/race-condition-issue
"""
import threading
import time

from django.core.cache import cache
from django.db import connections
from django.test import tag

from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State, RedisState

from tests.stability.base import (
    StabilityTestCase, run_concurrent, requires_real_redis,
)
from tests.stability.models import (
    Order, OrderProcess,
    side_effect_one, side_effect_two, side_effect_three,
)


def _is_sqlite():
    from django.conf import settings
    engine = settings.DATABASES.get('default', {}).get('ENGINE', '')
    return 'sqlite' in engine


@tag('stability')
class TestConcurrentTransitionRequests(StabilityTestCase):
    """
    2.1 -- Two concurrent requests for the same BackgroundTransition.

    Safety invariant: AT MOST one succeeds, no state corruption.
    With Postgres: exactly 1 succeeds, the rest are rejected.
    With SQLite: 0 or 1 may succeed (SQLite's DB-level write lock can
    cause the lock winner to fail on DB write), but the state is still
    consistent -- either the transition completed or nothing changed.
    """

    @requires_real_redis
    def test_two_threads_same_transition_at_most_one_wins(self):
        order = Order.objects.create(status='approved')
        barrier = threading.Barrier(2, timeout=5)

        def attempt():
            try:
                barrier.wait()
                fresh = Order.objects.get(pk=order.pk)
                process = OrderProcess(field_name='status', instance=fresh)
                process.fulfill()
                return 'success'
            except TransitionNotAllowed:
                return 'rejected'
            except Exception as e:
                return f'error:{e}'
            finally:
                connections.close_all()

        outcomes = run_concurrent(attempt, n_threads=2)

        successes = sum(
            1 for result, err in outcomes
            if err is None and result == 'success'
        )

        self.assertLessEqual(successes, 1,
            f"At most one thread should succeed. Outcomes: {outcomes}")

        order.refresh_from_db()
        if successes == 1:
            self.assertEqual(order.status, 'fulfilled')
        else:
            self.assertIn(order.status, ('approved', 'fulfillment_failed', 'fulfilling'),
                f"State must be consistent after contention. Got: {order.status}")

        if not _is_sqlite():
            self.assertEqual(successes, 1,
                f"With Postgres, exactly one thread must succeed. Outcomes: {outcomes}")

    @requires_real_redis
    def test_ten_threads_same_transition_at_most_one_wins(self):
        """Stress test: 10 concurrent requests, at most one succeeds."""
        order = Order.objects.create(status='approved')
        barrier = threading.Barrier(10, timeout=10)

        def attempt():
            try:
                barrier.wait()
                fresh = Order.objects.get(pk=order.pk)
                process = OrderProcess(field_name='status', instance=fresh)
                process.fulfill()
                return 'success'
            except TransitionNotAllowed:
                return 'rejected'
            except Exception as e:
                return f'error:{e}'
            finally:
                connections.close_all()

        outcomes = run_concurrent(attempt, n_threads=10)

        successes = sum(
            1 for result, err in outcomes
            if err is None and result == 'success'
        )

        self.assertLessEqual(successes, 1,
            f"At most one thread should succeed out of 10. Outcomes: {outcomes}")

        order.refresh_from_db()
        if successes == 1:
            self.assertEqual(order.status, 'fulfilled')
        else:
            self.assertIn(order.status, ('approved', 'fulfillment_failed', 'fulfilling'),
                f"State must be consistent after contention. Got: {order.status}")

        if not _is_sqlite():
            self.assertEqual(successes, 1,
                f"With Postgres, exactly one thread must succeed. Outcomes: {outcomes}")


@tag('stability')
@requires_real_redis
class TestRedisStateVisibility(StabilityTestCase):
    """
    2.2 -- RedisState makes in_progress_state visible immediately,
    before the DB transaction commits.

    This is the fix for the race condition documented in
    docs/research/race-condition-issue.

    NOTE: Requires real Redis. LocMemCache does not support nx=True
    for cache.set, so RedisState.lock() behaves incorrectly.
    """

    def test_redis_state_visible_before_db_commit(self):
        """
        When a lock is acquired via RedisState, the state change is
        visible to other threads immediately through Redis, even though
        the DB transaction hasn't committed yet.
        """
        order = Order.objects.create(status='approved')
        state = RedisState(order, 'status', process_name='process')
        self.track_lock(state)

        self.assertTrue(state.lock())

        state.set_state('fulfilling')

        self.assertTrue(state.is_locked())
        self.assertEqual(state.get_state(), 'fulfilling')

        other_state = RedisState(
            Order.objects.get(pk=order.pk), 'status', process_name='process'
        )
        self.assertTrue(other_state.is_locked())
        self.assertEqual(other_state.get_state(), 'fulfilling')

        state.unlock()

    def test_redis_state_blocks_second_lock_attempt(self):
        """After thread A locks via RedisState, thread B cannot lock."""
        order = Order.objects.create(status='approved')
        state_a = RedisState(order, 'status', process_name='process')
        self.track_lock(state_a)

        self.assertTrue(state_a.lock())

        state_b = RedisState(
            Order.objects.get(pk=order.pk), 'status', process_name='process'
        )
        self.assertFalse(state_b.lock())

        state_a.unlock()

    def test_concurrent_redis_lock_only_one_wins(self):
        """Two threads try to lock the same RedisState simultaneously."""
        order = Order.objects.create(status='approved')
        barrier = threading.Barrier(2, timeout=5)
        results = []
        lock = threading.Lock()

        def try_lock():
            try:
                s = RedisState(
                    Order.objects.get(pk=order.pk),
                    'status', process_name='process'
                )
                barrier.wait()
                got_lock = s.lock()
                with lock:
                    results.append(got_lock)
                if got_lock:
                    time.sleep(0.05)
                    s.unlock()
            finally:
                connections.close_all()

        outcomes = run_concurrent(try_lock, n_threads=2)

        self.assertEqual(results.count(True), 1,
            f"Exactly one thread should acquire the lock. Results: {results}")
        self.assertEqual(results.count(False), 1)


@tag('stability')
class TestRaceBetweenUserActionAndBackground(StabilityTestCase):
    """
    2.5 -- While a background transition is in_progress, a user tries
    to trigger a different transition on the same instance.

    Must be rejected: state is locked (via cache) and/or the current
    state (in_progress_state) is not in the new transition's sources.
    """

    def test_user_action_rejected_while_in_progress(self):
        order = Order.objects.create(status='fulfilling')
        state = State(order, 'status', process_name='process')

        self.assertTrue(state.lock())
        self.track_lock(state)

        process = OrderProcess(field_name='status', instance=order)
        available = list(process.get_available_transitions(action_name='cancel'))
        self.assertEqual(len(available), 0,
            "No transitions should be available while state is locked")

        state.unlock()
        self._tracked_cache_keys.discard(state._get_hash())

    def test_different_transition_rejected_when_locked(self):
        """Even if source state matches, the lock prevents execution."""
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        self.assertTrue(state.lock())

        process = OrderProcess(field_name='status', instance=order)
        with self.assertRaises(TransitionNotAllowed) as ctx:
            process.ship()
        self.assertIn("locked", str(ctx.exception).lower())

        state.unlock()
        self._tracked_cache_keys.discard(state._get_hash())


@tag('stability')
class TestRapidSuccessiveTransitions(StabilityTestCase):
    """
    2.6 -- Complete transition A, then immediately trigger transition B.

    No lock leaks, no stale cache, both complete correctly.
    """

    def test_sequential_transitions_no_lock_leak(self):
        order = Order.objects.create(status='draft')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        process = OrderProcess(field_name='status', instance=order)
        process.approve()

        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')
        self.assert_unlocked(state)

        process2 = OrderProcess(field_name='status', instance=order)
        process2.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')
        self.assert_unlocked(state)

    def test_three_rapid_transitions(self):
        order = Order.objects.create(status='draft')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        process = OrderProcess(field_name='status', instance=order)
        process.approve()
        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')

        process = OrderProcess(field_name='status', instance=order)
        process.fulfill()
        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')

        process = OrderProcess(field_name='status', instance=order)
        process.complete()
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')

        self.assert_unlocked(state)
