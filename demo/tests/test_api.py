import unittest

try:
    from rest_framework.test import APITestCase
    from rest_framework import status
    HAS_DRF = True
except ImportError:
    HAS_DRF = False

from django.urls import include, path, reverse
from demo.models import Lock


@unittest.skipUnless(HAS_DRF, "djangorestframework not installed")
class InvoiceAPITestCase(APITestCase if HAS_DRF else unittest.TestCase):
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
