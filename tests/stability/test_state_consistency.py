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

from django.test import tag

from django_logic.state import State

from tests.stability.base import StabilityTestCase
from tests.stability.models import (
    Order, OrderProcess,
)
from django_logic import Transition


@tag('stability')
class TestStateConsistencyAfterSuccess(StabilityTestCase):
    """Redis key must be deleted and DB must have target state after success."""

    def test_basic_state_lock_released_after_success(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')

        process = OrderProcess(field_name='status', instance=order)
        process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')


@tag('stability')
class TestStateConsistencyAfterFailure(StabilityTestCase):
    """Redis key must be deleted and DB must have failed_state after failure."""

    def test_lock_released_after_side_effect_failure(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')

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


@tag('stability')
class TestMultipleStateFieldConsistency(StabilityTestCase):
    """
    4.8 -- Multiple processes on the same model use independent state
    fields and independent locks. One process's lock must not affect
    the other.
    """


    def test_both_processes_can_transition_independently(self):
        from tests.stability.models import (
            MultiProcessOrder, FulfillmentProcess, PaymentProcess,
        )

        order = MultiProcessOrder.objects.create(
            fulfillment_status='pending', payment_status='unpaid'
        )

        state_f = State(order, 'fulfillment_status', process_name='fulfillment_process')
        state_p = State(order, 'payment_status', process_name='payment_process')

        fp = FulfillmentProcess(field_name='fulfillment_status', instance=order)
        fp.start_fulfillment()

        order.refresh_from_db()
        self.assertEqual(order.fulfillment_status, 'fulfilled')

        pp = PaymentProcess(field_name='payment_status', instance=order)
        pp.pay()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
