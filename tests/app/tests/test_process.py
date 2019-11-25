from django.test import TestCase

from app.models import Invoice
from app.process import InvoiceProcess


class ProcessTestCase(TestCase):
    def setUp(self):
        self.process_class = InvoiceProcess

    def test_process_class_method(self):
        self.assertEqual(self.process_class.get_process_name(), 'Invoice Process')

    def test_invoice_process(self):
        invoice = Invoice.objects.create(status='draft')
        invoice.status_process.approve()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'approved')
