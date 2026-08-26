"""Definitions and settings the engine must refuse or expose.

Class-creation and bind-time validation (shadowed names, bare-string
sources, MTI bindings, the nested tree walk), the settings checks, the
public import surface, and the smaller call-surface pins.
"""
from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.template import Context
from django.template.engine import Engine
from django.test import TestCase, override_settings

from django_logic import Process, ProcessManager, Transition
from django_logic.checks import check_no_unknown_settings
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


class UnknownSettingsCheckTests(TestCase):
    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERROR': 3,      # typo: missing S
    })
    def test_typo_is_reported_as_w004(self):
        """A misspelled key used to be ignored, and the default applied without
        a word."""
        findings = check_no_unknown_settings(None)
        self.assertEqual([f.id for f in findings], ['django_logic.W004'])
        self.assertIn('TRANSITION_MESSAGE_MAX_ERROR', findings[0].msg)

    @override_settings(DJANGO_LOGIC={'BACKGROUND_EXECUTION': 'sync'})
    def test_known_keys_are_silent(self):
        self.assertEqual(check_no_unknown_settings(None), [])

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'PHASE2_STATE_GUARD': 'enforce',        # removed in 0.10.0
    })
    def test_removed_keys_get_the_migration_advice_not_the_typo_hint(self):
        findings = check_no_unknown_settings(None)
        self.assertEqual(len(findings), 1)
        self.assertIn('removed', findings[0].msg)
        self.assertNotIn('typo', findings[0].msg)

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'STRICT_HOOK_SIGNATURES': True,
        'STRICT_KWARGS_SERIALIZATION': True,
        'LEGACY_EXCEPTION_BASE': 'some.fork.TransitionNotAllowed',
    })
    def test_the_keys_removed_in_1_0_get_the_migration_advice(self):
        findings = check_no_unknown_settings(None)
        self.assertEqual(
            [f.id for f in findings], ['django_logic.W003'] * 3)


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


class ReservedUserIdKwargTests(TestCase):
    def test_caller_supplied_user_id_is_refused(self):
        """The worker pops ``user_id`` and replaces it with a live user, so a
        caller's value could never reach a hook. Refused at enqueue."""
        from django_logic.background.serializers import (
            KwargsSerializationError, serialize_kwargs,
        )

        with self.assertRaises(KwargsSerializationError) as ctx:
            serialize_kwargs({'user_id': 'my-own-data', 'other': 1})
        self.assertIn('user_id', str(ctx.exception))


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

    def test_the_removed_classes_name_their_replacement(self):
        import django_logic
        import django_logic.background

        with self.assertRaises(ImportError) as ctx:
            django_logic.Action
        self.assertIn('Transition', str(ctx.exception))
        with self.assertRaises(ImportError) as ctx:
            django_logic.background.BackgroundAction
        self.assertIn('BackgroundTransition', str(ctx.exception))
        # The command classes are not advertised, but a direct import from
        # django_logic.commands keeps working for existing consumers.
        # The import itself is the assertion: it must not raise.
        from django_logic.commands import (
            Callbacks, Conditions, Permissions, SideEffects,
        )


class BundleSwapTests(TestCase):
    def test_the_three_kept_swap_points_still_swap(self):
        """The consumer subclasses these three; the failure-callback and
        condition bundles are always the stock classes since 1.0.0."""
        from django_logic.commands import Callbacks, Permissions, SideEffects

        class LoudCallbacks(Callbacks):
            pass

        class LoudPermissions(Permissions):
            pass

        class LoudSideEffects(SideEffects):
            pass

        class Custom(Transition):
            callbacks_class = LoudCallbacks
            permissions_class = LoudPermissions
            side_effects_class = LoudSideEffects

        t = Custom('go', sources=['draft'], target='approved')
        self.assertIsInstance(t.callbacks, LoudCallbacks)
        self.assertIsInstance(t.permissions, LoudPermissions)
        self.assertIsInstance(t.side_effects, LoudSideEffects)
        self.assertFalse(hasattr(t, 'failure_callbacks_class'))
        self.assertFalse(hasattr(t, 'conditions_class'))


