"""A lock check opens a connection of its own only inside a transaction."""
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TransactionTestCase

from django_logic.lock_ownership import _lock_connection
from django_logic.state import State
from tests.models import Invoice
from tests.stability.base import requires_postgres


@requires_postgres
class LockCheckConnectionTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.invoice = Invoice.objects.create(status='draft')
        self.holder = self.state()

    def state(self):
        return State(Invoice.objects.get(pk=self.invoice.pk), 'status')

    def test_a_check_without_a_transaction_reads_through_the_default_connection(self):
        checker = self.state()
        for locked in (True, False):
            with self.subTest(locked=locked):
                if locked:
                    self.assertTrue(self.holder.lock())
                else:
                    self.holder.unlock()
                with patch('django_logic.lock_ownership._lock_connection', wraps=_lock_connection) as extra:
                    self.assertEqual(checker.is_locked(), locked)
                extra.assert_not_called()

    def test_a_check_in_a_transaction_sees_a_lock_committed_after_its_snapshot(self):
        checker = self.state()
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            self.assertFalse(checker.is_locked())
            Invoice.objects.count()
            self.assertTrue(self.holder.lock())
            self.assertTrue(checker.is_locked())
        self.holder.unlock()
