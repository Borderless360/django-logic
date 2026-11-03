"""Definitions and settings the engine must refuse or expose.

Class-creation and bind-time validation (shadowed names, bare-string
sources, MTI bindings, the nested tree walk), the settings checks, the
public import surface, and the smaller call-surface pins.
"""
from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.template import Context
from django.template.engine import Engine
from django.test import TestCase

from django_logic import Process, ProcessManager, Transition
from tests.models import Invoice


def _noop(instance, **kwargs):
    pass


class _BindCleanup:
    """Unbind whatever the test bound, so the global registry stays clean."""

    _bound: tuple = ()

    def tearDown(self):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class not in self._bound
        ]
        for proc in self._bound:
            if proc.process_name in vars(Invoice):
                delattr(Invoice, proc.process_name)
        super().tearDown()


# --- Nested-process tree walk ---------------------------------------------

class SharedLeafProcess(Process):
    process_name = 'shared_leaf'
    transitions = [
        Transition('leaf_act', sources=['draft'], target='approved'),
    ]


class LeftBranchProcess(Process):
    process_name = 'left_branch'
    nested_processes = [SharedLeafProcess]
    transitions = []


class RightBranchProcess(Process):
    process_name = 'right_branch'
    nested_processes = [SharedLeafProcess]
    transitions = []


class DiamondRootProcess(Process):
    process_name = 'diamond_root'
    nested_processes = [LeftBranchProcess, RightBranchProcess]
    transitions = []


class DuplicateNestedProcess(Process):
    process_name = 'dup_nested'
    nested_processes = [SharedLeafProcess, SharedLeafProcess]
    transitions = []


class NestedTreeWalkTests(_BindCleanup, TestCase):
    _bound = (DiamondRootProcess, DuplicateNestedProcess)

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_diamond_yields_each_transition_once_and_is_callable(self):
        """A leaf reachable by two paths yielded its transitions twice, so
        resolution refused the single declaration as 'several transitions
        available' while get_available_actions still advertised it."""
        ProcessManager.bind_model_process(
            Invoice, DiamondRootProcess, state_field='status')
        inv = Invoice.objects.create(status='draft')

        names = [t.action_name
                 for t in inv.diamond_root.get_available_transitions()]
        self.assertEqual(names, ['leaf_act'])
        # Whatever is advertised must be callable.
        self.assertIn('leaf_act', inv.diamond_root.get_available_actions())
        inv.diamond_root.leaf_act()
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'approved')

    def test_duplicate_nested_entry_is_harmless(self):
        """The copy-paste mistake: the same class listed twice."""
        ProcessManager.bind_model_process(
            Invoice, DuplicateNestedProcess, state_field='status')
        inv = Invoice.objects.create(status='draft')
        inv.dup_nested.leaf_act()
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'approved')

    def test_nested_cycle_does_not_recurse_forever(self):
        """A nesting cycle A -> B -> A used to die with RecursionError."""
        class CycleB(Process):
            process_name = 'cycle_b'
            transitions = [
                Transition('b_act', sources=['draft'], target='approved'),
            ]

        class CycleA(Process):
            process_name = 'cycle_a'
            nested_processes = [CycleB]
            transitions = [
                Transition('a_act', sources=['draft'], target='approved'),
            ]

        CycleB.nested_processes = [CycleA]
        self._bound = self._bound + (CycleA,)
        ProcessManager.bind_model_process(
            Invoice, CycleA, state_field='status')
        inv = Invoice.objects.create(status='draft')

        self.assertEqual(
            sorted(inv.cycle_a.get_available_actions()), ['a_act', 'b_act'])
        inv.cycle_a.a_act()
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'approved')


# --- Templates must not drive the state machine ---------------------------

class TemplateSafeProcess(Process):
    process_name = 'tpl_safe_proc'
    transitions = [
        Transition('approve', sources=['draft'], target='approved'),
    ]


class TemplateRenderTests(_BindCleanup, TestCase):
    _bound = (TemplateSafeProcess,)

    def test_rendering_a_transition_does_not_execute_it(self):
        """Django calls any callable a template resolves, so
        ``{{ obj.process.approve }}`` used to run the transition while rendering
        a page, and print the transition id into the output."""
        ProcessManager.bind_model_process(
            Invoice, TemplateSafeProcess, state_field='status')
        cache.clear()
        inv = Invoice.objects.create(status='draft')

        out = Engine(libraries={}).from_string(
            '[{{ inv.tpl_safe_proc.approve }}]'
        ).render(Context({'inv': inv}))

        self.assertEqual(out, '[]')
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'draft')


# --- Definitions the engine must refuse -----------------------------------

class ShadowedDefinitionTests(TestCase):
    def test_action_name_shadowed_by_process_attribute_is_rejected(self):
        """Such a transition was advertised and did nothing, because
        __getattr__ runs only when the normal attribute lookup fails."""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            class Shadowed(Process):
                process_name = 'shadowed_proc'
                transitions = [
                    Transition('is_valid', sources=['draft'], target='approved'),
                ]

        self.assertIn('is_valid', str(ctx.exception))
        self.assertIn('shadowed', str(ctx.exception).lower())

    def test_process_name_colliding_with_model_field_is_rejected(self):
        """Binding replaced the field's descriptor with a read-only property,
        after which the model could not be created at all."""
        class FieldClash(Process):
            process_name = 'status'          # Invoice.status is a real field
            transitions = [
                Transition('go', sources=['draft'], target='approved'),
            ]

        with self.assertRaises(ImproperlyConfigured) as ctx:
            ProcessManager.bind_model_process(
                Invoice, FieldClash, state_field='status')
        self.assertIn('already names something', str(ctx.exception))


# --- Smaller fixes --------------------------------------------------------

class BareStringSourcesTests(TestCase):
    def test_sources_as_a_bare_string_is_rejected(self):
        """`list('draft')` is ['d','r','a','f','t'], which matches no state.
        get_available_actions() stopped listing the transition, and calling it
        reported a missing action instead of a bad declaration."""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            Transition('go', sources='draft', target='approved')
        self.assertIn('iterated per character', str(ctx.exception))

    def test_a_list_of_one_state_is_still_fine(self):
        t = Transition('go', sources=['draft'], target='approved')
        self.assertEqual(t.sources, ['draft'])


class PublicSurfaceTests(TestCase):
    def test_star_import_exposes_only_the_documented_names(self):
        """Without __all__, `from django_logic import *` leaked whichever
        submodules happened to be imported, so the namespace varied with
        INSTALLED_APPS."""
        import django_logic

        self.assertEqual(sorted(django_logic.__all__), [
            'Process', 'ProcessManager', 'Transition',
        ])
        for name in django_logic.__all__:
            self.assertTrue(hasattr(django_logic, name), name)

    def test_command_bundle_imports_remain_available(self):
        # The command classes are not advertised, but a direct import from
        # django_logic.commands keeps working for existing consumers.
        # The import itself is the assertion: it must not raise.
        from django_logic.commands import (
            Callbacks, Conditions, Permissions, SideEffects,
        )






# --- failed_state on a transition that cannot fail -------------------------

class FailedStateWithoutSideEffectsTests(TestCase):
    """A synchronous transition with no side effects has nothing that can
    raise between the lock and the target write, so a ``failed_state`` on it
    is never written. It reads as if a failure path exists; refuse it at
    declaration time (#279)."""

    def test_a_synchronous_transition_with_no_side_effects_refuses_it(self):
        with self.assertRaisesRegex(ImproperlyConfigured, 'no side effects'):
            Transition('start', sources=['pending'], target='in_progress', failed_state='pending')

    def test_with_a_side_effect_it_is_accepted(self):
        Transition('start', sources=['pending'], target='in_progress',
                   failed_state='pending', side_effects=[_noop])

    def test_a_background_transition_keeps_it(self):
        # The worker can fail before the target write — restoring the
        # instance, or the write itself — so the state is reachable there.
        from django_logic.background import BackgroundTransition
        BackgroundTransition('start', sources=['pending'], target='done', failed_state='failed')
