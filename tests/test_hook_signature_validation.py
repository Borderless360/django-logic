"""Bind-time hook-signature validation: a task-style
``def hook(*args, **kwargs)`` fails only at runtime on the worker, so
``bind_model_process`` refuses every hook whose first parameter is not a
named positional. It raises ``ImproperlyConfigured`` at bind time —
there is no lenient mode."""
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from django_logic.process import Process, ProcessManager
from django_logic.transition import Transition
from tests.models import Invoice


def good_hook(instance, **kwargs):
    pass


def task_style_hook(*args, **kwargs):
    pass


def kwargs_only_hook(**kwargs):
    pass


class _GoodProcess(Process):
    process_name = 'sig_ok_process'
    transitions = [
        Transition('approve', sources=['draft'], target='approved',
                   side_effects=[good_hook], callbacks=[good_hook],
                   conditions=[lambda instance: True]),
    ]


class _NestedBad(Process):
    transitions = [
        Transition('reject', sources=['draft'], target='rejected',
                   callbacks=[kwargs_only_hook]),
    ]


class _BadProcess(Process):
    process_name = 'sig_bad_process'
    nested_processes = [_NestedBad]
    transitions = [
        Transition('approve', sources=['draft'], target='approved',
                   side_effects=[task_style_hook]),
    ]


class _ProcessLevelBad(Process):
    process_name = 'sig_proc_level_process'
    conditions = [kwargs_only_hook]
    permissions = [task_style_hook]
    transitions = [
        Transition('approve', sources=['draft'], target='approved'),
    ]


class _DuckTransition:
    """Custom transition exposing only what it needs — the validator must
    not require the full hook-attribute surface."""
    action_name = 'quack'


class _DuckProcess(Process):
    process_name = 'sig_duck_process'
    transitions = [_DuckTransition()]


class HookSignatureValidationTests(SimpleTestCase):
    def tearDown(self):
        for name in ('sig_ok_process', 'sig_bad_process',
                     'sig_proc_level_process', 'sig_duck_process'):
            if name in vars(Invoice):
                delattr(Invoice, name)
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings if b.model is not Invoice]
        super().tearDown()

    def test_clean_hooks_bind(self):
        ProcessManager.bind_model_process(Invoice, _GoodProcess, state_field='status')

    def test_task_style_hooks_raise_at_bind_time(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            ProcessManager.bind_model_process(Invoice, _BadProcess, state_field='status')
        message = str(ctx.exception)
        # Both the direct and the nested offender, each with its owner.
        self.assertIn('task_style_hook', message)
        self.assertIn('_BadProcess.approve', message)
        self.assertIn('kwargs_only_hook', message)
        self.assertIn('_NestedBad.reject', message)
        self.assertIn('fn(instance, **kwargs)', message)

    def test_process_level_conditions_and_permissions_are_validated(self):
        # Process.is_valid executes class-level conditions/permissions with
        # the same instance-first convention — they must not escape the walk.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            ProcessManager.bind_model_process(
                Invoice, _ProcessLevelBad, state_field='status')
        message = str(ctx.exception)
        self.assertIn('kwargs_only_hook', message)
        self.assertIn('task_style_hook', message)
        self.assertIn('_ProcessLevelBad', message)

    def test_duck_typed_transition_without_hook_attributes_binds(self):
        # A transition object exposing only part of the hook surface must
        # not crash bind_model_process.
        ProcessManager.bind_model_process(
            Invoice, _DuckProcess, state_field='status')


class PropertyConditionsRegressionTests(SimpleTestCase):
    def test_process_with_property_conditions_binds(self):
        # A subclass may compute conditions per instance via a property; at
        # class level that is a non-iterable descriptor and must be skipped,
        # not crash the bind (issue #121).
        class DynamicConditionsProcess(Process):
            process_name = 'sig_dynamic_process'
            transitions = [
                Transition('approve', sources=['draft'], target='approved',
                           side_effects=[good_hook]),
            ]

            @property
            def conditions(self):
                return []

        try:
            ProcessManager.bind_model_process(Invoice, DynamicConditionsProcess,
                                              state_field='status')
        finally:
            if 'sig_dynamic_process' in vars(Invoice):
                delattr(Invoice, 'sig_dynamic_process')
            ProcessManager.bindings = [
                b for b in ProcessManager.bindings if b.model is not Invoice]
