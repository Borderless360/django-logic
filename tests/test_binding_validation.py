"""ProcessManager.bind_model_process validation.

Binding used to accept anything: repeated binds duplicated registry
entries, a (model, process_name) collision silently overwrote the model
property while the registry kept both claims, and a typo'd state_field
only failed deep inside a transition.

The ambiguous-recovery guardrails (retired in 0.12.0) that used to
live here were retired in 0.12.0 with ``recover_stranded_states``:
``in_progress_state`` is background-only now, written atomically with the
``TransitionMessage`` row, so recovery works from that row and marker sharing is
harmless — pinned below.
"""
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from django_logic.process import Process, ProcessManager
from django_logic.transition import Transition
from django_logic.background import BackgroundTransition
from tests.models import Invoice


class _MachineA(Process):
    process_name = 'machine_a'
    transitions = [
        Transition('run_a', sources=['draft'], target='done',
                   failed_state='a_failed'),
    ]


class _MachineB(Process):
    process_name = 'machine_b'
    transitions = [
        Transition('run_b', sources=['draft'], target='ready',
                   failed_state='b_failed'),
    ]


class _SharedMarkerOne(Process):
    process_name = 'shared_marker_one'
    transitions = [
        BackgroundTransition(
            'bg_one', sources=['draft'], target='done',
            in_progress_state='working', failed_state='one_failed'),
    ]


class _SharedMarkerTwo(Process):
    process_name = 'shared_marker_two'
    transitions = [
        BackgroundTransition(
            'bg_two', sources=['draft'], target='ready',
            in_progress_state='working', failed_state='two_failed'),
    ]


class _BindingCleanupMixin:
    _test_processes = ()

    def tearDown(self):
        for proc in self._test_processes:
            ProcessManager.unbind_model_process(Invoice, proc)
        super().tearDown()


class BindValidationTests(_BindingCleanupMixin, TestCase):
    _test_processes = (_MachineA, _MachineB)

    def test_identical_rebind_is_idempotent(self):
        ProcessManager.bind_model_process(Invoice, _MachineA, state_field='status')
        ProcessManager.bind_model_process(Invoice, _MachineA, state_field='status')
        entries = [
            b for b in ProcessManager.bindings if b.process_class is _MachineA
        ]
        self.assertEqual(len(entries), 1)

    def test_conflicting_process_name_rejected(self):
        ProcessManager.bind_model_process(Invoice, _MachineA, state_field='status')

        class _Impostor(Process):
            process_name = 'machine_a'
            transitions = []

        with self.assertRaises(ImproperlyConfigured):
            ProcessManager.bind_model_process(
                Invoice, _Impostor, state_field='status')
        # Same class re-bound onto a DIFFERENT field is a conflict too —
        # one accessor cannot serve two fields.
        with self.assertRaises(ImproperlyConfigured):
            ProcessManager.bind_model_process(
                Invoice, _MachineA, state_field='customer_received')

    def test_unknown_state_field_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            ProcessManager.bind_model_process(
                Invoice, _MachineB, state_field='no_such_field')
        self.assertEqual(
            [b for b in ProcessManager.bindings if b.process_class is _MachineB],
            [],
        )


class SharedMarkerIsLegalTests(_BindingCleanupMixin, TestCase):
    """The ambiguous-recovery check stays retired (0.12.0): two bound
    processes sharing a background ``in_progress_state`` with *divergent*
    recovery — the exact topology the retired check
    used to reject — now bind cleanly and pass ``manage.py check``. Every
    marked instance carries its transition on the ``TransitionMessage`` row,
    so there is no record-less recovery for claimants to disagree about.
    """

    _test_processes = (_SharedMarkerOne, _SharedMarkerTwo)

    def test_divergent_sharing_binds_and_checks_clean(self):
        from django.core import checks as django_checks

        ProcessManager.bind_model_process(
            Invoice, _SharedMarkerOne, state_field='status')
        ProcessManager.bind_model_process(
            Invoice, _SharedMarkerTwo, state_field='status')

        findings = django_checks.run_checks(tags=['django_logic'])
        self.assertEqual(
            findings, [],
            'the ambiguous-recovery check was retired in 0.12.0; '
            'two declarations may share an in-progress state freely',
        )
