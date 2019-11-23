from django.test import TestCase

from tests.app.models import Invoice
from tests.app.process import InvoiceProcess


class ProcessTestCase(TestCase):
    def setUp(self):
        self.process_class = InvoiceProcess

    def test_process_class_method(self):
        self.assertEqual(self.process_class.get_process_name(), 'Invoice Process')

    def test_invoice_process(self):
        invoice = Invoice.objects.create(reference='123', status='draft')
        invoice.status_process.approve()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'approved')
