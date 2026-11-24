"""A lock check opens a connection of its own only inside a transaction.

A listing of available actions reads the lock of its record once.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase

from django_logic.lock_ownership import _lock_connection
from django_logic.state import State
from tests.background.models import Widget
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
            # The first query on the default connection fixes the transaction's snapshot.
            Invoice.objects.count()
            self.assertTrue(self.holder.lock())
            self.assertTrue(checker.is_locked())
        self.holder.unlock()


class LockReadPerListingTests(TestCase):
    """``Widget.proc_guard`` declares ``go`` and nests a process that declares ``inner_go``."""

    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create(username='listing_staff', is_staff=True)
        self.widget = Widget.objects.create(status='draft', kwargs_seen=['gate_open'])

    def test_a_listing_reads_the_lock_once_for_a_process_and_its_nested_process(self):
        with patch('django_logic.lock_ownership.is_locked', return_value=False) as lock_reads:
            self.assertEqual(self.widget.proc_guard.get_available_actions(user=self.staff), ['go', 'inner_go'])
        self.assertEqual(lock_reads.call_count, 1)

    def test_one_lock_read_hides_the_locked_transitions_of_both_processes(self):
        with patch('django_logic.lock_ownership.is_locked', return_value=True) as lock_reads:
            self.assertEqual(self.widget.proc_guard.get_available_actions(user=self.staff), [])
        self.assertEqual(lock_reads.call_count, 1)

    def test_each_listing_reads_the_lock_again(self):
        with patch('django_logic.lock_ownership.is_locked', side_effect=[True, False]) as lock_reads:
            self.assertEqual(self.widget.proc_guard.get_available_actions(user=self.staff), [])
            self.assertEqual(self.widget.proc_guard.get_available_actions(user=self.staff), ['go', 'inner_go'])
        self.assertEqual(lock_reads.call_count, 2)
