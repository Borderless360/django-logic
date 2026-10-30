"""The business-row lock waits briefly, refuses a long holder, keeps the
failed state after a database error in a side effect, and lets a producer
on an earlier version insert job rows."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Timer
from unittest.mock import patch

from django.db import IntegrityError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.fairness import FairnessGroup
from django_logic.background.models import TransitionMessage
from django_logic.background.pull import claim_next
from django_logic.background.runner import finalize_stuck_attempt, run_background_transition
from django_logic.exceptions import RefusalReason, TransitionTemporarilyUnavailable
from tests import dl_settings
from tests.background.models import Widget
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


class BackgroundRowLockProcess(Process):
    process_name = 'background_row_lock_proc'
    transitions = [
        BackgroundTransition(
            'atomic_job', sources=['draft'], target='done', in_progress_state='working',
            failed_state='failed', side_effects=[record_success],
            failure_callbacks=[record_failure],
        ),
        BackgroundTransition(
            'non_atomic_job', sources=['draft'], target='done', in_progress_state='working',
            failed_state='failed', side_effects=[record_success],
            failure_callbacks=[record_failure], atomic=False,
        ),
    ]


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(
    BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_MAX_ERRORS=1,
))
class BackgroundTerminalRowLockTests(TransactionTestCase):
    def setUp(self):
        ProcessManager.bind_model_process(Invoice, BackgroundRowLockProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Invoice, BackgroundRowLockProcess)
        self.invoice = Invoice.objects.create(status='draft')

    def enqueue(self, action):
        getattr(self.invoice.background_row_lock_proc, action)()
        return TransitionMessage.objects.get(instance_id=str(self.invoice.pk))

    def run_while_held(self, message, *, finalize=False, release_at_completion=False):
        """Keep a newer state uncommitted until the completion check or test cleanup."""
        from django_logic.background import runner

        locked, release, finished = Event(), Event(), Event()
        complete = runner._complete_terminal_failure

        def holder():
            try:
                with transaction.atomic():
                    Invoice.objects.select_for_update().get(pk=self.invoice.pk)
                    Invoice.objects.filter(pk=self.invoice.pk).update(status='manual')
                    locked.set()
                    if not release.wait(10):
                        raise AssertionError('Test did not release the business row')
            finally:
                connections.close_all()

        def worker():
            try:
                if finalize:
                    return finalize_stuck_attempt(message.pk)
                return run_background_transition(message.pk)
            finally:
                connections.close_all()
                finished.set()

        with ThreadPoolExecutor(max_workers=2) as executor, \
                patch('django_logic.state.ROW_LOCK_WAIT_SECONDS', 0.1):
            holding = executor.submit(holder)
            self.assertTrue(locked.wait(5))

            def complete_after_commit(*args, **kwargs):
                job, transition, state = args[:3]
                self.assertEqual(job._state.db, state.instance._state.db)
                if not transition.atomic:
                    self.assertNotEqual(job._state.db, 'default')
                    self.assertEqual(kwargs['using'], state.instance._state.db)
                if release_at_completion:
                    release.set()
                    holding.result(timeout=5)
                return complete(*args, **kwargs)

            with patch.object(runner, '_complete_terminal_failure', complete_after_commit):
                running = executor.submit(worker)
                try:
                    bounded = finished.wait(2)
                finally:
                    # Release before joining even when the old implementation blocks.
                    release.set()
                    holding.result(timeout=5)
                    running.result(timeout=5)
        self.assertTrue(bounded, 'Completion waited for the other transaction instead of its bounded lock wait')
        self.invoice.refresh_from_db()
        message.refresh_from_db()
        self.assertEqual(self.invoice.status, 'manual')
        self.assertFalse(Invoice.objects.filter(status='cb-ran').exists())
        self.assertEqual(message.errors_count, 1)

    def assert_refused_completion(self, action, *, finalize=False):
        message = self.enqueue(action)
        if finalize:
            message.record_error(ValueError('earlier attempt failed'))
        self.run_while_held(message, finalize=finalize)
        self.assertTrue(message.is_completed)
        self.assertTrue(message.ended_in_failure)
        self.assertEqual(message.completion_kind, 'failed')
        self.assertIn('failed_state protection', message.failure_side_effect_error)
        self.assertEqual(Invoice.objects.filter(status='done').count(), int(action == 'non_atomic_job'))

    def assert_changed_state_completion(self, action, *, finalize=False):
        message = self.enqueue(action)
        if finalize:
            message.record_error(ValueError('earlier attempt failed'))
        self.run_while_held(message, finalize=finalize, release_at_completion=True)
        self.assertTrue(message.is_completed)
        self.assertFalse(message.ended_in_failure)
        self.assertEqual(message.completion_kind, 'superseded')
        self.assertIn('manual', message.last_error_message)
        self.assertEqual(Invoice.objects.filter(status='done').count(), int(action == 'non_atomic_job'))

    def test_final_atomic_attempt_finishes_while_business_row_remains_locked(self):
        self.assert_refused_completion('atomic_job')

    def test_final_non_atomic_attempt_preserves_committed_work_while_business_row_is_locked(self):
        self.assert_refused_completion('non_atomic_job')

    def test_stuck_finalization_finishes_while_business_row_remains_locked(self):
        self.assert_refused_completion('atomic_job', finalize=True)

    def test_final_atomic_attempt_rechecks_state_after_the_holder_commits(self):
        self.assert_changed_state_completion('atomic_job')

    def test_final_non_atomic_attempt_rechecks_state_after_the_holder_commits(self):
        self.assert_changed_state_completion('non_atomic_job')

    def test_stuck_finalization_rechecks_state_after_the_holder_commits(self):
        self.assert_changed_state_completion('atomic_job', finalize=True)

    @override_settings(DJANGO_LOGIC=dl_settings(
        BACKGROUND_EXECUTION='pull', TRANSITION_MESSAGE_MAX_ERRORS=2,
    ))
    def test_nonfinal_lock_refusal_keeps_the_normal_retry(self):
        message = self.enqueue('atomic_job')
        self.run_while_held(message)
        self.assertFalse(message.is_completed)
        self.assertEqual(message.failure_side_effect_error, '')
        self.assertFalse(Invoice.objects.filter(status='done').exists())

    def test_deleted_business_row_keeps_the_recorded_failure_and_completes(self):
        from django_logic.background import runner

        message = self.enqueue('atomic_job')
        restore = runner._restore_for_attempt

        def delete_in_another_connection():
            try:
                Invoice.objects.filter(pk=self.invoice.pk).delete()
            finally:
                connections.close_all()

        def restore_then_delete(job):
            restored = restore(job)
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(delete_in_another_connection).result(timeout=5)
            return restored

        with patch.object(runner, '_restore_for_attempt', restore_then_delete):
            run_background_transition(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.errors_count, 1)
        self.assertTrue(message.is_completed)
        self.assertEqual(message.completion_kind, 'failed')
        self.assertIn('DoesNotExist', message.failure_side_effect_error)
        self.assertFalse(Invoice.objects.filter(status='cb-ran').exists())

    def test_database_error_checking_terminal_state_keeps_the_recorded_failure(self):
        from django_logic.background import runner

        message = self.enqueue('atomic_job')

        def failed_check(*args):
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1 / 0')

        with patch.object(runner, '_state_guard_matches', failed_check):
            run_background_transition(message.pk)

        message.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertEqual(message.errors_count, 1)
        self.assertTrue(message.is_completed)
        self.assertEqual(message.completion_kind, 'failed')
        self.assertIn('division by zero', message.last_error_message)
        self.assertIn('division by zero', message.failure_side_effect_error)
        self.assertEqual(self.invoice.status, 'working')
        self.assertFalse(Invoice.objects.filter(status='cb-ran').exists())


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


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class EarlierProducerUpgradeTests(TransactionTestCase):
    app_label = 'django_logic_background'
    queue = 'django_logic.critical'

    def setUp(self):
        executor = MigrationExecutor(connection)
        self.latest = executor.loader.graph.leaf_nodes(self.app_label)
        self.addCleanup(self.migrate, self.latest)
        old_state = self.migrate([(self.app_label, '0010_proxy_model_label')])
        # This model generates the INSERT an earlier producer would send. It
        # knows none of the new columns, even after this test upgrades the schema.
        self.older_message = old_state.apps.get_model(self.app_label, 'TransitionMessage')
        self.widget = Widget.objects.create(status='fulfilling')

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets)

    def enqueue_from_earlier_producer(self):
        return self.older_message.objects.create(
            app_label='bg_tests', model_name='widget', instance_id=str(self.widget.pk),
            process_name='process', field_name='status', transition_name='fulfil',
            queue_name=self.queue, kwargs={},
        ).pk

    def assert_new_worker_completes(self, pk):
        message = TransitionMessage.objects.get(pk=pk)
        self.assertEqual(
            (message.request_key, message.request_fingerprint, message.fairness_key,
             message.eligible_at, message.deferral_reason, message.completion_kind,
             message.required_next),
            (None, '', '', None, '', '', ''),
        )
        self.assertEqual(claim_next([self.queue]), pk)
        run_background_transition(pk)
        self.widget.refresh_from_db()
        message.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilled')
        self.assertEqual(self.widget.se_log, 'ok,')
        self.assertEqual(self.widget.cb_log, 'cb,')
        self.assertTrue(message.is_completed)
        self.assertEqual(message.errors_count, 0)
        self.assertEqual(message.completion_kind, 'succeeded')
        self.assertIsNone(claim_next([self.queue]))

    def test_earlier_producer_inserts_after_0011_before_the_remaining_migrations(self):
        self.migrate([(self.app_label, '0011_operation_contract')])
        pk = self.enqueue_from_earlier_producer()
        self.migrate(self.latest)

        self.assertTrue(FairnessGroup.objects.filter(key='').exists())
        self.assert_new_worker_completes(pk)

    def test_earlier_producer_inserts_after_the_upgrade_without_a_fairness_group(self):
        self.migrate(self.latest)
        self.assertFalse(FairnessGroup.objects.exists())
        pk = self.enqueue_from_earlier_producer()
        self.assertFalse(FairnessGroup.objects.exists())

        self.assert_new_worker_completes(pk)
        self.assertTrue(FairnessGroup.objects.filter(key='').exists())

    def test_0013_repairs_published_300_defaults_before_the_new_worker_runs(self):
        self.migrate([(self.app_label, '0012_fairness_groups')])
        quote = connection.ops.quote_name
        table = quote(TransitionMessage._meta.db_table)
        # The published 3.0.0 migration dropped all five defaults. Reproduce that
        # schema without replacing the checked-in migration or its recorded state.
        with connection.cursor() as cursor:
            for column in ['request_fingerprint', 'fairness_key', 'deferral_reason',
                           'completion_kind', 'required_next']:
                cursor.execute(f'ALTER TABLE {table} ALTER COLUMN {quote(column)} DROP DEFAULT')
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.enqueue_from_earlier_producer()

        self.migrate(self.latest)
        self.assert_new_worker_completes(self.enqueue_from_earlier_producer())
