"""
Category 4.3: State Consistency Between Redis and DB

After every terminal state (success, failure, crash+recovery), Redis
and the database must agree. Redis keys must be cleaned up after
transitions complete.

This module tests the contract:
  - After successful transition: Redis key deleted, DB has target state
  - After failed transition: Redis key deleted, DB has failed_state
  - After lock timeout expiry: Redis key expires, DB has in_progress_state
  - During active transition: Redis reflects in_progress_state
"""
import time

from django.core.cache import cache
from django.test import tag

from django_logic.state import State, RedisState

from tests.stability.base import StabilityTestCase, requires_real_redis
from tests.stability.models import (
    Order, OrderProcess,
    side_effect_one, side_effect_two, side_effect_three,
    failure_side_effect,
)
from django_logic import Transition


@tag('stability')
class TestStateConsistencyAfterSuccess(StabilityTestCase):
    """Redis key must be deleted and DB must have target state after success."""

    def test_basic_state_lock_released_after_success(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        process = OrderProcess(field_name='status', instance=order)
        process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')
        self.assert_unlocked(state)
        self.assertIsNone(self.get_cache_value(state))

    @requires_real_redis
    def test_redis_state_cleared_after_success(self):
        order = Order.objects.create(status='approved')
        state = RedisState(order, 'status', process_name='process')
        self.track_lock(state)

        self.assertTrue(state.lock())
        state.set_state('fulfilling')
        self.assertTrue(state.is_locked())

        state.set_state('fulfilled')
        state.unlock()

        self.assertFalse(state.is_locked())
        self.assertIsNone(cache.get(state._get_hash()))

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')


@tag('stability')
class TestStateConsistencyAfterFailure(StabilityTestCase):
    """Redis key must be deleted and DB must have failed_state after failure."""

    def test_lock_released_after_side_effect_failure(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        def failing(instance, **kwargs):
            raise ValueError("fail")

        process_cls = type('FailProcess', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    failed_state='fulfillment_failed',
                    side_effects=[failing],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(ValueError):
            process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfillment_failed')
        self.assert_unlocked(state)
        self.assertIsNone(self.get_cache_value(state))

    @requires_real_redis
    def test_redis_state_cleared_after_failure(self):
        order = Order.objects.create(status='approved')
        state = RedisState(order, 'status', process_name='process')
        self.track_lock(state)

        self.assertTrue(state.lock())
        state.set_state('fulfilling')

        state.set_state('fulfillment_failed')
        state.unlock()

        self.assertFalse(state.is_locked())
        self.assertIsNone(cache.get(state._get_hash()))
        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfillment_failed')


@tag('stability')
class TestStateConsistencyDuringTransition(StabilityTestCase):
    """During an active transition, Redis must reflect the current state."""

    @requires_real_redis
    def test_redis_state_shows_in_progress_during_side_effects(self):
        order = Order.objects.create(status='approved')
        observed_states = []

        def observing_side_effect(instance, **kwargs):
            s = RedisState(
                Order.objects.get(pk=instance.pk),
                'status', process_name='process',
            )
            observed_states.append(s.get_state())

        state = RedisState(order, 'status', process_name='process')
        self.track_lock(state)

        process_cls = type('ObservingProcess', (OrderProcess,), {
            'state_class': RedisState,
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    side_effects=[observing_side_effect],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        process.fulfill()

        self.assertEqual(observed_states, ['fulfilling'])

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')


@tag('stability')
class TestStateConsistencyAfterLockExpiry(StabilityTestCase):
    """After lock expires, Redis key is gone but DB retains in_progress_state."""

    def test_expired_lock_state_divergence(self):
        """
        After TTL expiry, the Redis key is gone but the DB still has
        in_progress_state. This is the scenario the periodic starter
        must detect and handle.
        """
        order = Order.objects.create(status='fulfilling')
        state = State(order, 'status', process_name='process')

        self.assertFalse(state.is_locked())

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilling')

    def test_redis_state_falls_back_to_db_after_expiry(self):
        order = Order.objects.create(status='fulfilling')
        state = RedisState(order, 'status', process_name='process')

        self.assertFalse(state.is_locked())
        self.assertEqual(state.get_state(), 'fulfilling')


@tag('stability')
class TestMultipleStateFieldConsistency(StabilityTestCase):
    """
    4.8 -- Multiple processes on the same model use independent state
    fields and independent locks. One process's lock must not affect
    the other.
    """

    def test_independent_locks_for_different_state_fields(self):
        from tests.stability.models import (
            MultiProcessOrder, FulfillmentProcess, PaymentProcess,
        )

        order = MultiProcessOrder.objects.create(
            fulfillment_status='pending', payment_status='unpaid'
        )

        state_f = State(order, 'fulfillment_status', process_name='fulfillment_process')
        state_p = State(order, 'payment_status', process_name='payment_process')
        self.track_lock(state_f)
        self.track_lock(state_p)

        self.assertTrue(state_f.lock())
        self.assertTrue(state_p.lock())

        self.assertTrue(state_f.is_locked())
        self.assertTrue(state_p.is_locked())

        state_f.unlock()
        self.assertFalse(state_f.is_locked())
        self.assertTrue(state_p.is_locked())

        state_p.unlock()
        self.assertFalse(state_p.is_locked())

    def test_both_processes_can_transition_independently(self):
        from tests.stability.models import (
            MultiProcessOrder, FulfillmentProcess, PaymentProcess,
        )

        order = MultiProcessOrder.objects.create(
            fulfillment_status='pending', payment_status='unpaid'
        )

        state_f = State(order, 'fulfillment_status', process_name='fulfillment_process')
        state_p = State(order, 'payment_status', process_name='payment_process')
        self.track_lock(state_f)
        self.track_lock(state_p)

        fp = FulfillmentProcess(field_name='fulfillment_status', instance=order)
        fp.start_fulfillment()

        order.refresh_from_db()
        self.assertEqual(order.fulfillment_status, 'fulfilled')
        self.assert_unlocked(state_f)

        pp = PaymentProcess(field_name='payment_status', instance=order)
        pp.pay()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
        self.assert_unlocked(state_p)
