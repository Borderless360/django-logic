from django.test import TestCase

from demo.models import Invoice
from demo.process import InvoiceProcess


class InvoiceProcessTestCase(TestCase):
    def setUp(self):
        self.process_class = InvoiceProcess

    def test_process_class_method(self):
        self.assertEqual(self.process_class.get_readable_name(), 'Invoice Process')

    def test_invoice_process(self):
        invoice = Invoice.objects.create(status='draft')
        invoice.invoice_process.approve()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'approved')