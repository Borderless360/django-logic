"""Best-effort hooks vs the caller's database transaction (#138).

Callbacks (success and failure), failure side-effects, and
``next_transition`` all swallow exceptions by contract. Swallowing a
*database* error inside an open ``transaction.atomic()`` block does not
heal the connection — Django marks it rollback-only, so every later ORM
call raised ``TransactionManagementError`` and the transition's own state
write could roll back with the caller. Since #138 each hook is isolated
in a savepoint when (and only when) the caller is inside an open
transaction, and one failed callback no longer prevents the rest of the
callback list from running.
"""
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase

from django_logic.process import Process, ProcessManager
from django_logic.transition import Transition
from tests.models import Invoice

RECORDED = []


def _poison_db(instance, **kwargs):
    # A genuine IntegrityError: status is NOT NULL.
    RECORDED.append('poison_attempt')
    Invoice.objects.create(status=None)


def _record(instance, **kwargs):
    RECORDED.append('record')


def _boom(instance, **kwargs):
    raise ValueError('side effect failed')


class _HookProcess(Process):
    process_name = 'hook_process'
    transitions = [
        Transition('approve', sources=['draft'], target='approved',
                   callbacks=[_poison_db, _record]),
        Transition('explode', sources=['draft'], target='done',
                   failed_state='failed',
                   side_effects=[_boom],
                   failure_callbacks=[_poison_db, _record]),
        Transition('cleanup_breaks', sources=['draft'], target='done',
                   failed_state='failed',
                   side_effects=[_boom],
                   failure_side_effects=[_poison_db]),
        Transition('chain', sources=['draft'], target='approved',
                   next_transition='poisoned_followup'),
        Transition('poisoned_followup', sources=['approved'], target='done',
                   side_effects=[_poison_db]),
    ]


def _process(invoice):
    return _HookProcess(field_name='status', instance=invoice)


class CallbackIsolationInTransactionTests(TestCase):
    """TestCase wraps every test in an atomic block — exactly the
    poisoning scenario (ATOMIC_REQUESTS / service-layer atomics)."""

    def setUp(self):
        super().setUp()
        RECORDED.clear()
        self.invoice = Invoice.objects.create(status='draft')

    def _assert_connection_healthy(self):
        # Pre-#138 this raised TransactionManagementError after a
        # swallowed IntegrityError.
        Invoice.objects.create(status='healthy')

    def test_failing_success_callback_does_not_poison_or_block_later_callbacks(self):
        _process(self.invoice).approve()

        self.assertEqual(RECORDED, ['poison_attempt', 'record'])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'approved')
        self._assert_connection_healthy()

    def test_failing_failure_callback_does_not_poison_or_block_later_callbacks(self):
        with self.assertRaises(ValueError):
            _process(self.invoice).explode()

        self.assertEqual(RECORDED, ['poison_attempt', 'record'])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'failed')
        self._assert_connection_healthy()

    def test_failing_failure_side_effect_does_not_poison_the_transaction(self):
        with self.assertRaises(ValueError):
            _process(self.invoice).cleanup_breaks()

        self.invoice.refresh_from_db()
        # failed_state was written before the cleanup broke, and survives.
        self.assertEqual(self.invoice.status, 'failed')
        self._assert_connection_healthy()

    def test_failing_next_transition_does_not_poison_the_transaction(self):
        # next_transition resolves the process from the bound model
        # attribute, so this one test needs a real binding.
        ProcessManager.bind_model_process(
            Invoice, _HookProcess, state_field='status')
        try:
            self.invoice.hook_process.chain()
        finally:
            ProcessManager.bindings = [
                b for b in ProcessManager.bindings
                if b.process_class is not _HookProcess
            ]
            if 'hook_process' in vars(Invoice):
                delattr(Invoice, 'hook_process')

        self.invoice.refresh_from_db()
        # The follow-up's side effect blew up (swallowed, best-effort) —
        # the first transition's target stands and the connection works.
        self.assertEqual(self.invoice.status, 'approved')
        self._assert_connection_healthy()

    def test_transition_state_write_survives_poisoned_callback_commit(self):
        # The full write path: run inside an explicit inner atomic and
        # verify the state write is still there after it exits (it would
        # have been rolled back with a poisoned connection).
        with transaction.atomic():
            _process(self.invoice).approve()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'approved')


class CallbackIsolationAutocommitTests(TransactionTestCase):
    """Outside a transaction there is nothing to poison: no savepoints,
    later callbacks still run after an earlier failure."""

    def test_later_callbacks_run_after_a_failure(self):
        RECORDED.clear()
        invoice = Invoice.objects.create(status='draft')

        _HookProcess(field_name='status', instance=invoice).approve()

        self.assertEqual(RECORDED, ['poison_attempt', 'record'])
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'approved')
