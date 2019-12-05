from django.test import TestCase
from demo.models import Invoice, Order
from django_logic.transition import Transition


class TransitionTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')  # uses mixin
        self.order = Order.objects.create(payment_status='paid')  # doesn't use mixin
        self.transition = Transition('test', sources=[], target='cancelled')

    def test_get_hash(self):
        self.assertEqual(self.transition._get_hash(self.order, 'payment_status'),
                         'demo-order-payment_status-{}'.format(self.order.pk))
        self.assertEqual(self.transition._get_hash(self.invoice, 'status'),
                         'demo-invoice-status-{}'.format(self.invoice.pk))

    def test_get_db_state(self):
        self.assertEqual(self.transition._get_db_state(self.invoice, 'status'), 'draft')
        self.assertEqual(self.transition._get_db_state(self.order, 'payment_status'), 'paid')
    
    def test_lock(self):
        self.assertFalse(self.transition._is_locked(self.invoice, 'status'))
        self.assertFalse(self.transition._is_locked(self.order, 'payment_status'))
        self.transition._lock(self.invoice, 'status')
        self.transition._lock(self.order, 'payment_status')
        self.assertTrue(self.transition._is_locked(self.invoice, 'status'))
        self.assertTrue(self.transition._is_locked(self.order, 'payment_status'))

        # nothing should happen
        self.transition._lock(self.invoice, 'status')
        self.transition._lock(self.order, 'payment_status')
        self.assertTrue(self.transition._is_locked(self.invoice, 'status'))
        self.assertTrue(self.transition._is_locked(self.order, 'payment_status'))

        self.transition._unlock(self.invoice, 'status')
        self.transition._unlock(self.order, 'payment_status')
        self.assertFalse(self.transition._is_locked(self.invoice, 'status'))
        self.assertFalse(self.transition._is_locked(self.order, 'payment_status'))

    def test_set_state(self):
        self.transition._set_state(self.order, 'payment_status', 'unpaid')
        self.transition._set_state(self.invoice, 'status', 'void')
        self.assertEqual(self.order.payment_status, 'unpaid')
        self.assertEqual(self.invoice.status, 'void')
        # make sure it was saved to db
        self.order.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'unpaid')
        self.assertEqual(self.invoice.status, 'void')

    def test_change_state(self):
        self.transition.change_state(self.invoice, 'status')
        self.assertEqual(self.invoice.status, self.transition.target)
        self.assertFalse(self.transition._is_locked(self.invoice, 'status'))

        self.transition.change_state(self.order, 'payment_status')
        self.assertEqual(self.order.payment_status, self.transition.target)
        self.assertFalse(self.transition._is_locked(self.order, 'payment_status'))


def disable_invoice(invoice: Invoice):
    invoice.is_available = False
    invoice.save()


def enable_invoice(invoice: Invoice):
    invoice.is_available = True
    invoice.save()


def fail_invoice(invoice: Invoice):
    raise Exception


class TransitionSideEffectsTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_side_effect(self):
        transition = Transition('test', sources=[], target='cancelled', side_effects=[disable_invoice])
        self.assertTrue(self.invoice.is_available)
        transition.change_state(self.invoice, 'status')
        self.assertEqual(self.invoice.status, transition.target)
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(transition._is_locked(self.invoice, 'status'))

    def test_many_side_effects(self):
        transition = Transition('test', sources=[], target='cancelled',
                                side_effects=[disable_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        transition.change_state(self.invoice, 'status')
        self.assertEqual(self.invoice.status, transition.target)
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(transition._is_locked(self.invoice, 'status'))

    def test_failure_during_side_effect(self):
        transition = Transition('test', sources=[], target='cancelled',
                                side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        transition.change_state(self.invoice, 'status')
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(transition._is_locked(self.invoice, 'status'))

    def test_failure_during_side_effect_with_failed_state(self):
        transition = Transition('test', sources=[], target='cancelled', failed_state='failed',
                                side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        transition.change_state(self.invoice, 'status')
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(transition._is_locked(self.invoice, 'status'))


class TransitionCallbacksTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_callback(self):
        transition = Transition('test', sources=[], target='cancelled', callbacks=[disable_invoice])
        self.assertTrue(self.invoice.is_available)
        transition.change_state(self.invoice, 'status')
        self.assertEqual(self.invoice.status, transition.target)
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(transition._is_locked(self.invoice, 'status'))

    def test_many_callbacks(self):
        transition = Transition('test', sources=[], target='cancelled',
                                callbacks=[disable_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        transition.change_state(self.invoice, 'status')
        self.assertEqual(self.invoice.status, transition.target)
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(transition._is_locked(self.invoice, 'status'))

    def test_failure_during_callbacks(self):
        transition = Transition('test', sources=[], target='cancelled',
                                callbacks=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        transition.change_state(self.invoice, 'status')
        self.assertEqual(self.invoice.status, 'cancelled')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(transition._is_locked(self.invoice, 'status'))

    def test_failure_during_callbacks_with_failed_state(self):
        transition = Transition('test', sources=[], target='cancelled', failed_state='failed',
                                side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        transition.change_state(self.invoice, 'status')
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(transition._is_locked(self.invoice, 'status'))