"""ProcessManager.bind_model_process validation + ambiguous-recovery
guardrails (#143).

Binding used to accept anything: repeated binds duplicated registry
entries, a (model, process_name) collision silently overwrote the model
property while the registry kept both claims, a typo'd state_field only
failed deep inside a transition, and two machines sharing an
in_progress_state on one field made record-less stranded recovery guess
an owner.
"""
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from django_logic.background.dispatch import recover_stranded_states
from django_logic.checks import check_unambiguous_in_progress_ownership
from django_logic.process import (
    Process,
    ProcessManager,
    collect_ambiguous_in_progress_states,
)
from django_logic.transition import Action, Transition
from tests.models import Invoice


class _MachineA(Process):
    process_name = 'machine_a'
    transitions = [
        Transition('run_a', sources=['draft'], target='done',
                   in_progress_state='working', failed_state='a_failed'),
    ]


class _MachineB(Process):
    process_name = 'machine_b'
    transitions = [
        Transition('run_b', sources=['draft'], target='ready',
                   in_progress_state='working', failed_state='b_failed'),
    ]


class _MachineC(Process):
    process_name = 'machine_c'
    transitions = [
        Transition('run_c', sources=['draft'], target='done',
                   in_progress_state='c_working', failed_state='c_failed'),
    ]


class _ActionOnlyMachine(Process):
    process_name = 'machine_action'
    transitions = [
        Action('poke', sources=['draft'], in_progress_state='working'),
    ]


class _BindingCleanupMixin:
    _test_processes = (_MachineA, _MachineB, _MachineC, _ActionOnlyMachine)

    def tearDown(self):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class not in self._test_processes
        ]
        for proc in self._test_processes:
            if proc.process_name in vars(Invoice):
                delattr(Invoice, proc.process_name)
        super().tearDown()


class BindValidationTests(_BindingCleanupMixin, TestCase):
    def test_identical_rebind_is_idempotent(self):
        ProcessManager.bind_model_process(Invoice, _MachineA, state_field='status')
        before = len(ProcessManager.bindings)
        ProcessManager.bind_model_process(Invoice, _MachineA, state_field='status')
        self.assertEqual(len(ProcessManager.bindings), before)
        # The model property still dispatches to the machine.
        invoice = Invoice.objects.create(status='draft')
        self.assertIsInstance(invoice.machine_a, _MachineA)

    def test_conflicting_process_name_rejected(self):
        ProcessManager.bind_model_process(Invoice, _MachineA, state_field='status')

        clash = type('_MachineAClash', (Process,), {
            'process_name': 'machine_a',
            'transitions': [Transition('x', sources=['a'], target='b')],
        })
        with self.assertRaisesMessage(ImproperlyConfigured, 'machine_a'):
            ProcessManager.bind_model_process(Invoice, clash, state_field='status')

        # Same class re-bound onto a DIFFERENT field is a conflict too —
        # one property name cannot dispatch to two fields.
        with self.assertRaisesMessage(ImproperlyConfigured, 'machine_a'):
            ProcessManager.bind_model_process(
                Invoice, _MachineA, state_field='customer_received')

    def test_unknown_state_field_rejected(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'no_such_field'):
            ProcessManager.bind_model_process(
                Invoice, _MachineA, state_field='no_such_field')
        self.assertEqual(
            [b for b in ProcessManager.bindings if b.process_class is _MachineA],
            [],
        )


class AmbiguousInProgressTests(_BindingCleanupMixin, TestCase):
    def _bind_shared_in_progress(self):
        ProcessManager.bind_model_process(Invoice, _MachineA, state_field='status')
        ProcessManager.bind_model_process(Invoice, _MachineB, state_field='status')

    def test_collector_flags_shared_in_progress_state(self):
        self._bind_shared_in_progress()
        ambiguous = collect_ambiguous_in_progress_states()
        self.assertIn(('tests.Invoice', 'status', 'working'), ambiguous)

    def test_distinct_in_progress_states_are_fine(self):
        ProcessManager.bind_model_process(Invoice, _MachineA, state_field='status')
        ProcessManager.bind_model_process(Invoice, _MachineC, state_field='status')
        self.assertEqual(collect_ambiguous_in_progress_states(), {})
        self.assertEqual(check_unambiguous_in_progress_ownership(None), [])

    def test_actions_do_not_claim_in_progress(self):
        # An Action never writes in_progress_state — it must not turn a
        # single legitimate owner into a conflict.
        ProcessManager.bind_model_process(Invoice, _MachineA, state_field='status')
        ProcessManager.bind_model_process(
            Invoice, _ActionOnlyMachine, state_field='status')
        self.assertEqual(collect_ambiguous_in_progress_states(), {})

    def test_system_check_reports_e001(self):
        self._bind_shared_in_progress()
        findings = check_unambiguous_in_progress_ownership(None)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, 'django_logic.E001')
        self.assertIn('working', findings[0].msg)
        self.assertIn('_MachineA.run_a', findings[0].msg)
        self.assertIn('_MachineB.run_b', findings[0].msg)

    def test_sweep_skips_ambiguous_state(self):
        self._bind_shared_in_progress()
        cache.clear()
        stranded = Invoice.objects.create(status='working')

        with self.assertLogs('django-logic', level='ERROR') as logs:
            recovered = recover_stranded_states()

        self.assertEqual(recovered, 0)
        stranded.refresh_from_db()
        # Parked, not guessed: neither a_failed nor b_failed was written.
        self.assertEqual(stranded.status, 'working')
        self.assertTrue(any('E001' in line for line in logs.output))
