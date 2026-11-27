"""Committed results of actions on one or more state fields."""
from django.test import tag


from tests.stability.base import StabilityTestCase
from tests.stability.models import (
    Order, OrderProcess,
)
from django_logic import Transition


@tag('stability')
class TestStateConsistencyAfterSuccess(StabilityTestCase):

    def test_target_persists_after_success(self):
        order = Order.objects.create(status='approved')

        process = OrderProcess(field_name='status', instance=order)
        process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')


@tag('stability')
class TestStateConsistencyAfterFailure(StabilityTestCase):

    def test_failure_state_persists_after_side_effect_error(self):
        order = Order.objects.create(status='approved')

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


        fp = FulfillmentProcess(field_name='fulfillment_status', instance=order)
        fp.start_fulfillment()

        order.refresh_from_db()
        self.assertEqual(order.fulfillment_status, 'fulfilled')

        pp = PaymentProcess(field_name='payment_status', instance=order)
        pp.pay()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
