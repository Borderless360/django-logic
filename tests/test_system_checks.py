"""The bindings registry.

A process with a bad hook signature never binds — bind_model_process
raises at bind time — so a machine in the registry always has clean
hooks.
"""
from django.core.checks import run_checks
from django.test import SimpleTestCase

from django_logic.process import ModelProcessBinding, Process, ProcessManager
from django_logic.transition import Transition
from tests.models import Invoice


def good_hook(instance, **kwargs):
    pass


def task_style_hook(*args, **kwargs):
    pass


class _CleanProcess(Process):
    process_name = 'checks_clean_process'
    transitions = [
        Transition('approve', sources=['draft'], target='approved',
                   side_effects=[good_hook]),
    ]


class _OffendingProcess(Process):
    process_name = 'checks_offending_process'
    transitions = [
        Transition('approve', sources=['draft'], target='approved',
                   side_effects=[task_style_hook]),
    ]


class BindingsRegistryTests(SimpleTestCase):
    def tearDown(self):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class not in (_CleanProcess, _OffendingProcess)
        ]
        for name in ('checks_clean_process', 'checks_offending_process'):
            if name in vars(Invoice):
                delattr(Invoice, name)
        super().tearDown()

    def test_bind_records_a_registry_entry(self):
        ProcessManager.bind_model_process(Invoice, _CleanProcess, state_field='status')
        self.assertIn(
            ModelProcessBinding(Invoice, _CleanProcess, 'status'),
            ProcessManager.bindings,
        )

    def test_an_offending_process_never_reaches_the_registry(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            ProcessManager.bind_model_process(
                Invoice, _OffendingProcess, state_field='status')
        self.assertNotIn(
            ModelProcessBinding(Invoice, _OffendingProcess, 'status'),
            ProcessManager.bindings,
        )

    def test_no_findings_for_clean_bindings(self):
        ProcessManager.bind_model_process(Invoice, _CleanProcess, state_field='status')
        self.assertEqual(
            [f for f in run_checks()
             if str(f.id).startswith('django_logic.')], [])
