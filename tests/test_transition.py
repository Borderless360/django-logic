from django.test import TestCase
from demo.models import Invoice
from django_logic.state import State
from django_logic.transition import Transition


def disable_invoice(invoice: Invoice):
    invoice.is_available = False
    invoice.save()


def update_invoice(invoice, is_available, customer_received):
    invoice.is_available = is_available
    invoice.customer_received = customer_received
    invoice.save()


def enable_invoice(invoice: Invoice):
    invoice.is_available = True
    invoice.save()


def fail_invoice(invoice: Invoice):
    raise Exception


def receive_invoice(invoice: Invoice):
    invoice.customer_received = True
    invoice.save()


class TransitionSideEffectsTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_side_effect(self):
        transition = Transition('test', sources=[], target='cancelled', side_effects=[disable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, transition.target)
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_many_side_effects(self):
        transition = Transition('test', sources=[], target='cancelled',
                                side_effects=[disable_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, transition.target)
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_side_effect(self):
        transition = Transition('test', sources=[], target='cancelled',
                                side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_side_effect_with_failed_state(self):
        transition = Transition('test', sources=[], target='cancelled', failed_state='failed',
                                side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_side_effect_with_parameters(self):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        transition = Transition('test', sources=[], target='cancelled', failed_state='failed',
                                side_effects=[update_invoice])
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        transition.change_state(state, is_available=False, customer_received=False)
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        self.assertFalse(state.is_locked())


class TransitionCallbacksTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_callback(self):
        transition = Transition('test', sources=[], target='cancelled', callbacks=[disable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, transition.target)
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_many_callbacks(self):
        transition = Transition('test', sources=[], target='cancelled',
                                callbacks=[disable_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, transition.target)
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_callbacks(self):
        transition = Transition('test', sources=[], target='cancelled',
                                callbacks=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'cancelled')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_callbacks_with_failed_state(self):
        transition = Transition('test', sources=[], target='cancelled', failed_state='failed',
                                side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_callbacks_with_parameters(self):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        transition = Transition('test', sources=[], target='cancelled', failed_state='failed',
                                callbacks=[update_invoice])
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        transition.change_state(state, is_available=False, customer_received=False)
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        self.assertFalse(state.is_locked())


class TransitionFailureCallbacksTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_callback(self):
        transition = Transition('test', sources=[], target='success', side_effects=[fail_invoice],
                                failure_callbacks=[disable_invoice], failed_state='failed')
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_many_callback(self):
        transition = Transition('test', sources=[], target='success', side_effects=[fail_invoice],
                                failure_callbacks=[disable_invoice, receive_invoice], failed_state='failed')
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        self.assertFalse(state.is_locked())

    def test_callbacks_with_parameters(self):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        transition = Transition('test', sources=[], target='success', failed_state='failed',
                                side_effects=[fail_invoice], failure_callbacks=[update_invoice])
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        transition.change_state(state, is_available=False, customer_received=False)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        self.assertFalse(state.is_locked())
