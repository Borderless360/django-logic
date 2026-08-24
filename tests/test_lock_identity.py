"""The state lock names one physical row and column.

Two class names can address one row: a proxy model and the model it
proxies, and a multi-table-inheritance child and the parent that
declares the state column. Both write the same column of the same row.
A key built from the class name gave them separate locks, so two
transitions ran on one row at the same time.
"""
from django.test import TestCase

from django_logic.state import State
from tests.models import Invoice, InvoiceProxy, MtiChild, MtiParent


class LockIdentityTests(TestCase):
    def test_a_proxy_and_the_model_it_proxies_share_one_key(self):
        invoice = Invoice.objects.create(status='draft')
        proxy = InvoiceProxy.objects.get(pk=invoice.pk)
        self.assertEqual(
            State(invoice, 'status').instance_key,
            State(proxy, 'status').instance_key,
        )

    def test_a_proxy_cannot_take_a_lock_the_concrete_model_holds(self):
        invoice = Invoice.objects.create(status='draft')
        proxy = InvoiceProxy.objects.get(pk=invoice.pk)
        held = State(invoice, 'status')
        self.assertTrue(held.lock())
        self.addCleanup(held.unlock)
        self.assertFalse(State(proxy, 'status').lock())

    def test_an_mti_child_shares_the_key_of_the_parent_that_declares_it(self):
        child = MtiChild.objects.create(status='draft')
        parent = MtiParent.objects.get(pk=child.pk)
        self.assertEqual(
            State(child, 'status').instance_key,
            State(parent, 'status').instance_key,
        )

    def test_two_rows_take_two_keys(self):
        first = Invoice.objects.create(status='draft')
        second = Invoice.objects.create(status='draft')
        self.assertNotEqual(
            State(first, 'status').instance_key,
            State(second, 'status').instance_key,
        )

    def test_two_columns_on_one_row_take_two_keys(self):
        invoice = Invoice.objects.create(status='draft')
        self.assertNotEqual(
            State(invoice, 'status').instance_key,
            State(invoice, 'customer_received').instance_key,
        )
