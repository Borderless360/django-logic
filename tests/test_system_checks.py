"""Bindings registry + the django_logic.W001 system check (#125): warn-mode
hook validation logs during ready(), before logging is configured, so the
checks framework is the surface that cannot be missed."""
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

    def test_w001_emitted_for_offending_hooks_only(self):
        ProcessManager.bind_model_process(Invoice, _CleanProcess, state_field='status')
        with self.assertLogs('django-logic.transition', level='WARNING'):
            ProcessManager.bind_model_process(Invoice, _OffendingProcess, state_field='status')

        findings = [f for f in run_checks() if f.id == 'django_logic.W001']
        self.assertEqual(len(findings), 1)
        self.assertIn('task_style_hook', findings[0].msg)
        self.assertIn('_OffendingProcess', findings[0].obj)

    def test_no_findings_when_all_bound_hooks_are_clean(self):
        ProcessManager.bind_model_process(Invoice, _CleanProcess, state_field='status')
        self.assertEqual([f for f in run_checks() if f.id == 'django_logic.W001'], [])
