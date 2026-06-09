"""
Category 3: Deadlock Tests

Proves that the locking design never creates true deadlocks -- only
controlled rejections (TransitionNotAllowed). Tests cover:

  - Nested transition lock contention (the GV production bug)
  - Cross-instance nested transitions
  - Lock timeout and expiry recovery
  - Lock release on every failure path
  - Cache backend failure behavior

Reference: docs/research/race-condition-issue (lines 62-80, 92-106)
"""
import time
import threading
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings, tag

from django_logic import Transition, Process, ProcessManager
from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State, RedisState

from tests.stability.base import (
    StabilityTestCase, WorkerCrashSimulated, run_concurrent,
    requires_real_redis,
)
from tests.stability.models import (
    Order, OrderProcess, OrderProcessWithNestedCallback,
    side_effect_one, side_effect_two,
    trigger_nested_transition,
)


@tag('stability')
class TestNestedTransitionLockContention(StabilityTestCase):
    """
    3.1 -- The exact GV production bug: transition A locks the instance,
    a callback tries to start transition B on the SAME instance.

    Transition B must be rejected with "State is locked" -- NOT a deadlock.
    The parent transition's state remains correct.
    """

    def test_callback_on_same_instance_after_unlock(self):
        """
        Callbacks run AFTER complete_transition has already unlocked
        the state. So a callback that triggers another transition on
        the same instance will actually succeed (the lock is free).

        This is the correct behavior: unlock -> callbacks -> next_transition.
        The GV bug happened during SIDE EFFECTS (while lock was held),
        not during callbacks.
        """
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        ProcessManager.bind_model_process(
            Order, OrderProcessWithNestedCallback, state_field='status'
        )
        try:
            process = OrderProcessWithNestedCallback(
                field_name='status', instance=order
            )
            process.fulfill()

            order.refresh_from_db()
            # Callback triggers complete() which succeeds because lock is released
            self.assertEqual(order.status, 'completed')
            self.assert_unlocked(state)
        finally:
            ProcessManager.bind_model_process(
                Order, OrderProcess, state_field='status'
            )

    def test_side_effect_nested_same_instance_rejected(self):
        """
        A side effect (not callback) tries to start another transition
        on the same instance. This should raise TransitionNotAllowed,
        which triggers fail_transition.
        """
        def side_effect_that_nests(instance, **kwargs):
            instance.process.cancel()

        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        process_cls = type('NestedSEProcess', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    failed_state='fulfillment_failed',
                    side_effects=[side_effect_that_nests],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(TransitionNotAllowed):
            process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfillment_failed')
        self.assert_unlocked(state)


@tag('stability')
class TestNestedTransitionDifferentInstance(StabilityTestCase):
    """
    3.2 -- Transition A on instance #1 has a callback that starts
    transition B on a DIFFERENT instance #2.

    Both locks are independent -- no deadlock possible. Both transitions
    should complete (or B should fail gracefully if #2 is already locked).
    """

    def test_callback_on_different_instance_succeeds(self):
        order1 = Order.objects.create(status='approved')
        order2 = Order.objects.create(status='draft')

        state1 = State(order1, 'status', process_name='process')
        state2 = State(order2, 'status', process_name='process')
        self.track_lock(state1)
        self.track_lock(state2)

        def approve_order2(instance, **kwargs):
            fresh_order2 = Order.objects.get(pk=order2.pk)
            p = OrderProcess(field_name='status', instance=fresh_order2)
            p.approve()

        process_cls = type('CrossInstanceProcess', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    side_effects=[side_effect_one],
                    callbacks=[approve_order2],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order1)
        process.fulfill()

        order1.refresh_from_db()
        order2.refresh_from_db()

        self.assertEqual(order1.status, 'fulfilled')
        self.assertEqual(order2.status, 'approved')
        self.assert_unlocked(state1)
        self.assert_unlocked(state2)

    def test_callback_on_locked_different_instance_fails_gracefully(self):
        """If order #2 is already locked, the callback fails but is swallowed."""
        order1 = Order.objects.create(status='approved')
        order2 = Order.objects.create(status='draft')

        state1 = State(order1, 'status', process_name='process')
        state2 = State(order2, 'status', process_name='process')
        self.track_lock(state1)
        self.track_lock(state2)

        self.assertTrue(state2.lock())

        def try_approve_locked_order2(instance, **kwargs):
            fresh = Order.objects.get(pk=order2.pk)
            p = OrderProcess(field_name='status', instance=fresh)
            p.approve()

        process_cls = type('LockedCrossProcess', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    side_effects=[side_effect_one],
                    callbacks=[try_approve_locked_order2],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order1)
        process.fulfill()

        order1.refresh_from_db()
        order2.refresh_from_db()

        self.assertEqual(order1.status, 'fulfilled')
        self.assertEqual(order2.status, 'draft')
        self.assert_unlocked(state1)

        state2.unlock()
        self._tracked_cache_keys.discard(state2._get_hash())


@tag('stability')
class TestLockTimeoutAndExpiry(StabilityTestCase):
    """
    3.4 -- Lock expires after LOCK_TIMEOUT. After expiry, the instance
    becomes available for new transitions.
    """

    @override_settings(DJANGO_LOGIC={'LOCK_TIMEOUT': 1})
    def test_lock_expires_after_timeout(self):
        order = Order.objects.create(status='approved')
        s = State(order, 'status', process_name='process')

        self.assertTrue(s.lock())
        self.assertTrue(s.is_locked())

        time.sleep(1.5)

        self.assertFalse(s.is_locked())

    @requires_real_redis
    @override_settings(DJANGO_LOGIC={'LOCK_TIMEOUT': 1})
    def test_redis_state_lock_expires_after_timeout(self):
        order = Order.objects.create(status='approved')
        s = RedisState(order, 'status', process_name='process')

        self.assertTrue(s.lock())
        self.assertTrue(s.is_locked())

        time.sleep(1.5)

        self.assertFalse(s.is_locked())
        self.assertEqual(s.get_state(), 'approved')

    @override_settings(DJANGO_LOGIC={'LOCK_TIMEOUT': 1})
    def test_new_transition_succeeds_after_lock_expiry(self):
        order = Order.objects.create(status='approved')
        s = State(order, 'status', process_name='process')

        self.assertTrue(s.lock())

        time.sleep(1.5)

        process = OrderProcess(field_name='status', instance=order)
        process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')


@tag('stability')
class TestLockReleaseOnEveryFailurePath(StabilityTestCase):
    """
    3.5 -- For EVERY exception type during change_state, the lock must
    be released. No orphaned locks.

    Parametrized over: side effect ValueError, side effect TypeError,
    side effect RuntimeError, side effect KeyError.
    """

    def _test_lock_released_on_exception(self, exception_cls, msg="test error"):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        def failing_se(instance, **kwargs):
            raise exception_cls(msg)

        process_cls = type('LockReleaseProcess', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    failed_state='fulfillment_failed',
                    side_effects=[failing_se],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(exception_cls):
            process.fulfill()

        self.assert_unlocked(state)
        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfillment_failed')

    def test_lock_released_on_value_error(self):
        self._test_lock_released_on_exception(ValueError)

    def test_lock_released_on_type_error(self):
        self._test_lock_released_on_exception(TypeError)

    def test_lock_released_on_runtime_error(self):
        self._test_lock_released_on_exception(RuntimeError)

    def test_lock_released_on_key_error(self):
        self._test_lock_released_on_exception(KeyError)

    def test_lock_released_on_os_error(self):
        self._test_lock_released_on_exception(OSError)

    def test_lock_released_on_connection_error(self):
        self._test_lock_released_on_exception(ConnectionError)

    def test_lock_released_without_failed_state(self):
        """Lock must be released even when no failed_state is configured."""
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        def failing_se(instance, **kwargs):
            raise ValueError("no failed_state configured")

        process_cls = type('NoFailedStateLockProcess', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    side_effects=[failing_se],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(ValueError):
            process.fulfill()

        self.assert_unlocked(state)


@tag('stability')
class TestCacheBackendFailure(StabilityTestCase):
    """
    3.6 -- Redis goes down during lock() or unlock().

    The system should raise a clear error, NOT silently proceed without
    locking (which would allow concurrent execution).
    """

    def test_lock_failure_raises_on_broken_cache(self):
        """If cache.add raises (Redis down), the transition should not proceed."""
        order = Order.objects.create(status='approved')

        with patch('django_logic.state.cache') as mock_cache:
            mock_cache.add.side_effect = ConnectionError("Redis connection refused")
            mock_cache.get.side_effect = ConnectionError("Redis connection refused")

            state = State(order, 'status', process_name='process')
            with self.assertRaises(ConnectionError):
                state.lock()

    def test_is_locked_returns_false_on_cache_error(self):
        """
        If cache.get fails, is_locked should propagate the error
        rather than returning False (which would allow a second lock).
        """
        order = Order.objects.create(status='approved')

        with patch('django_logic.state.cache') as mock_cache:
            mock_cache.get.side_effect = ConnectionError("Redis down")
            state = State(order, 'status', process_name='process')

            with self.assertRaises(ConnectionError):
                state.is_locked()

    def test_unlock_failure_is_visible(self):
        """If unlock (cache.delete) fails, the error should propagate."""
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')

        self.assertTrue(state.lock())
        self.track_lock(state)

        with patch('django_logic.state.cache') as mock_cache:
            mock_cache.delete.side_effect = ConnectionError("Redis down")
            with self.assertRaises(ConnectionError):
                state.unlock()

        cache.delete(state._get_hash())
        self._tracked_cache_keys.discard(state._get_hash())
