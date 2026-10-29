"""The business-row lock waits briefly, refuses a long holder, keeps the
failed state after a database error in a side effect, and lets a producer
on an earlier version insert job rows."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Timer
from unittest.mock import patch

from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from django_logic import Process, ProcessManager, Transition
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import RefusalReason, TransitionTemporarilyUnavailable
from tests.models import Invoice
from tests.stability.base import requires_postgres


def duplicate_primary_key(invoice, **kwargs):
    Invoice.objects.create(pk=invoice.pk, status='duplicate')


def record_failure(invoice, **kwargs):
    Invoice.objects.create(status='cb-ran')


def record_success(invoice, **kwargs):
    Invoice.objects.create(status='done')


class RowLockProcess(Process):
    process_name = 'row_lock_proc'
    transitions = [
        Transition('fail_in_database', sources=['draft'], target='done', failed_state='failed',
                   side_effects=[duplicate_primary_key], failure_callbacks=[record_failure]),
        Transition('approve', sources=['draft'], target='approved', side_effects=[record_success]),
    ]


@requires_postgres
class RowLockTests(TransactionTestCase):
    def setUp(self):
        ProcessManager.bind_model_process(Invoice, RowLockProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Invoice, RowLockProcess)
        self.invoice = Invoice.objects.create(status='draft')

    def hold_row(self, locked, release):
        """Hold the invoice row in another connection until released."""
        def hold():
            try:
                with transaction.atomic():
                    Invoice.objects.select_for_update().get(pk=self.invoice.pk)
                    locked.set()
                    release.wait(10)
            finally:
                connections.close_all()
        return hold

    def test_failed_state_and_failure_callback_survive_a_database_error(self):
        with self.assertRaises(IntegrityError):
            self.invoice.row_lock_proc.fail_in_database()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'failed')
        self.assertTrue(Invoice.objects.filter(status='cb-ran').exists())
        self.assertFalse(Invoice.objects.filter(status='duplicate').exists())

    def test_a_holder_that_commits_within_the_wait_does_not_refuse(self):
        locked, release = Event(), Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            holder = executor.submit(self.hold_row(locked, release))
            self.assertTrue(locked.wait(5))
            Timer(0.3, release.set).start()
            self.invoice.row_lock_proc.approve()
            holder.result(timeout=10)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'approved')
        self.assertTrue(Invoice.objects.filter(status='done').exists())

    def test_a_holder_beyond_the_wait_is_refused_and_logged(self):
        locked, release = Event(), Event()
        with ThreadPoolExecutor(max_workers=1) as executor, patch('django_logic.state.ROW_LOCK_WAIT_SECONDS', 0.2):
            holder = executor.submit(self.hold_row(locked, release))
            try:
                self.assertTrue(locked.wait(5))
                with self.assertLogs('django-logic', level='WARNING') as logs, \
                        self.assertRaises(TransitionTemporarilyUnavailable) as caught:
                    self.invoice.row_lock_proc.approve()
                self.assertEqual(caught.exception.reason, RefusalReason.LOCKED)
                self.assertIn('held the record for more than', logs.output[0])
            finally:
                release.set()
                holder.result(timeout=10)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(Invoice.objects.filter(status='done').exists())

    def test_the_caller_keeps_its_own_lock_timeout(self):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('lock_timeout', '7s', true)")
            self.invoice.row_lock_proc.approve()
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('lock_timeout')")
                self.assertEqual(cursor.fetchone()[0], '7s')


class EarlierProducerInsertTests(TestCase):
    def test_a_row_without_the_new_columns_gets_the_database_defaults(self):
        """A producer on 2.4 names none of the columns 3.0 added."""
        original = TransitionMessage.objects.create(
            app_label='tests', model_name='invoice', instance_id='1', process_name='p',
            field_name='status', transition_name='go', queue_name='q', kwargs={}, is_completed=True,
        )
        added = {'request_key', 'request_fingerprint', 'fairness_key', 'eligible_at',
                 'deferral_reason', 'completion_kind', 'required_next'}
        quote = connection.ops.quote_name
        columns = ', '.join(quote(field.column) for field in TransitionMessage._meta.concrete_fields
                            if field.column not in added and field.column != 'id')
        table = quote(TransitionMessage._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f'INSERT INTO {table} ({columns}) SELECT {columns} FROM {table} WHERE id = %s',
                           [original.pk])
        copy = TransitionMessage.objects.exclude(pk=original.pk).get()
        self.assertEqual(
            (copy.request_key, copy.request_fingerprint, copy.fairness_key, copy.eligible_at,
             copy.deferral_reason, copy.completion_kind, copy.required_next),
            (None, '', '', None, '', '', ''),
        )
