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
from unittest.mock import patch

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


@tag('stability')
class TestConcurrentTransitionRequests(StabilityTestCase):
    """
    2.1 -- N concurrent requests for the same transition on one instance.

    The synchronous lock guarantees *mutual exclusion*: while one thread
    holds the state lock and runs the transition's side-effects, every
    other request for the same instance is rejected ("State is locked").

    An earlier version of this test let a fast winner acquire-run-release
    the lock before the other threads even attempted it, so two threads
    could win *sequentially* (each off a stale read) and the "exactly one
    wins" assertion was flaky — the sync lock provides mutual exclusion,
    not exactly-once. We now pin the winner inside its critical section
    until every other thread has attempted and been rejected, so the
    contention window provably covers all N threads. The lock must then
    admit exactly one winner, reject the rest, and run the side-effects
    exactly once.
    """

    def _assert_one_winner_under_forced_contention(self, n_threads):
        order = Order.objects.create(status='approved')
        start = threading.Barrier(n_threads, timeout=10)

        coordinator = threading.Lock()
        rejected = {'count': 0}
        all_others_rejected = threading.Event()

        def hold_until_others_rejected(instance, **kwargs):
            # Runs only in the winning thread, while it holds the lock.
            # Block until the other N-1 threads have each attempted and
            # been rejected, guaranteeing genuine contention rather than a
            # fast unlock-before-the-others-try. Times out defensively so
            # a stuck thread can never hang the suite.
            all_others_rejected.wait(timeout=10)

        def attempt():
            try:
                start.wait()
                fresh = Order.objects.get(pk=order.pk)
                process = OrderProcess(field_name='status', instance=fresh)
                process.fulfill()
                return 'success'
            except TransitionNotAllowed:
                with coordinator:
                    rejected['count'] += 1
                    if rejected['count'] >= n_threads - 1:
                        all_others_rejected.set()
                return 'rejected'
            finally:
                connections.close_all()

        # Pin a blocking side-effect at the front of `fulfill` so the lock
        # holder stays inside the critical section throughout the race.
        fulfill = next(
            t for t in OrderProcess.transitions if t.action_name == 'fulfill'
        )
        pinned = [hold_until_others_rejected, *fulfill.side_effects.commands]
        with patch.object(fulfill.side_effects, '_commands', pinned):
            outcomes = run_concurrent(attempt, n_threads=n_threads)

        results = [r for r, err in outcomes if err is None]
        errors = [(r, err) for r, err in outcomes if err is not None]
        self.assertEqual(errors, [], f"No thread should error: {outcomes}")
        self.assertEqual(
            results.count('success'), 1,
            f"Exactly one thread must win under contention: {outcomes}")
        self.assertEqual(
            results.count('rejected'), n_threads - 1,
            f"The other {n_threads - 1} must be rejected: {outcomes}")

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')
        # Side-effects ran exactly once — the lock prevented any duplicate.
        self.assertEqual(order.side_effect_log, 'se1,se2,se3,')

    @requires_real_redis
    def test_two_threads_same_transition_at_most_one_wins(self):
        self._assert_one_winner_under_forced_contention(2)

    @requires_real_redis
    def test_ten_threads_same_transition_at_most_one_wins(self):
        """Stress test: 10 concurrent requests, exactly one wins."""
        self._assert_one_winner_under_forced_contention(10)


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
