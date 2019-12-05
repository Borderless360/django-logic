from django.urls import include, path, reverse
from rest_framework.test import APITestCase
from rest_framework import status

from demo.models import Invoice


class InvoiceAPITestCase(APITestCase):
    def test_create_invoice(self):
        url = reverse('demo:invoice-list')
        response = self.client.post(url, data={}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        invoice = Invoice.objects.last()
        self.assertEqual(response.json(), {'id': invoice.pk,
                                           'status': 'draft',
                                           'actions': ['approve', 'void'],
                                           'customer_received': False,
                                           'is_available': True})

    def test_get_invoices(self):
        url = reverse('demo:invoice-list')
        invoice = Invoice.objects.create(status='draft')
        response = self.client.get(url)
        self.assertEqual(response.json(),
                         [{'id': invoice.pk,
                           'status': 'draft',
                           'actions': ['approve', 'void'],
                           'customer_received': False,
                           'is_available': True}])
