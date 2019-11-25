from django.test import TestCase
from app.models import Invoice, Order
from django_logic.transition import Transition


class TransitionTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')  # uses mixin
        self.order = Order.objects.create(payment_status='paid')  # doesn't use mixin
        self.transition = Transition('test', sources=[], target='')

    def test_get_hash(self):
        self.assertEqual(self.transition._get_hash(self.order, 'payment_status'),
                         'app-order-payment_status-{}'.format(self.order.pk))
        self.assertEqual(self.transition._get_hash(self.invoice, 'status'),
                         'app-invoice-status-{}'.format(self.invoice.pk))

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