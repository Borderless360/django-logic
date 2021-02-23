from django.urls import include, path, reverse
from rest_framework.test import APITestCase
from rest_framework import status

from demo.models import Lock


class InvoiceAPITestCase(APITestCase):
    def test_create_invoice(self):
        url = reverse('demo:lock-list')
        response = self.client.post(url, data={}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        locker = Lock.objects.last()
        self.assertEqual(response.json(), {'id': locker.pk,
                                           'status': 'open',
                                           'actions': ['lock', 'refresh']})

    def test_get_invoices(self):
        url = reverse('demo:lock-list')
        invoice = Lock.objects.create()
        response = self.client.get(url)
        self.assertEqual(response.json(),
                         [{'id': invoice.pk,
                           'actions': ['lock', 'refresh'],
                           'status': 'open'}])
