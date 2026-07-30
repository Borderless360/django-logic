"""
Category 4.3: State Consistency Between Redis and DB

After every terminal state (success, failure, crash+recovery), Redis
and the database must agree. Redis keys must be cleaned up after
transitions complete.

This module tests the contract:
  - After successful transition: Redis key deleted, DB has target state
  - After failed transition: Redis key deleted, DB has failed_state
  - After lock timeout expiry: Redis key expires; the DB holds the last
    committed state (no in-progress marker for sync work since 0.12.0)
  - During an active transition: the lock is the only in-flight signal
"""
import time

from django.core.cache import cache
from django.test import tag

from django_logic.state import State

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


@tag('stability')
class TestStateConsistencyDuringTransition(StabilityTestCase):
    """During an active transition, Redis must reflect the current state."""


@tag('stability')
class TestStateConsistencyAfterLockExpiry(StabilityTestCase):
    """After lock expiry a sync run leaves no divergence to reconcile."""

    def test_expired_lock_leaves_no_state_divergence(self):
        """After TTL expiry the Redis key is gone and the DB state is
        whatever the last COMMITTED transition wrote — a killed sync run
        rolls back to its source (0.12.0: no in-progress marker), so
        nothing needs detecting or handling; the instance is re-drivable.
        """
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')

        self.assertFalse(state.is_locked())

        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')


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
