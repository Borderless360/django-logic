"""Definitions and settings the engine must refuse or expose.

Class-creation and bind-time validation (shadowed names, bare-string
sources, MTI bindings, the nested tree walk), the settings checks, the
public import surface, and the smaller call-surface pins.
"""
from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.template import Context
from django.template.engine import Engine
from django.test import TestCase, TransactionTestCase, override_settings

from django_logic import Process, ProcessManager, Transition
from django_logic.conf import (
    strict_kwargs_serialization,
    validate_bool,
)
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


class BooleanSettingTests(TestCase):
    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'STRICT_KWARGS_SERIALIZATION': 'false',
    })
    def test_string_false_does_not_enable_strict_mode(self):
        """The value went through bool(), so the string 'false' — an environment
        variable read straight through — switched strict mode on."""
        self.assertFalse(strict_kwargs_serialization())
        with self.assertRaises(ImproperlyConfigured):
            validate_bool('STRICT_KWARGS_SERIALIZATION')

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'STRICT_KWARGS_SERIALIZATION': True,
    })
    def test_literal_true_enables_strict_mode(self):
        self.assertTrue(strict_kwargs_serialization())
        validate_bool('STRICT_KWARGS_SERIALIZATION')   # must not raise


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


class DeferredUnlockRegistryTests(TransactionTestCase):
    """The registry registered its on_commit clear only while it was empty. A
    rollback discards the hook but keeps the entries, so the registry was never
    empty again, never cleared, and grew for the life of the connection."""

    def test_registry_clears_after_a_rollback_then_commits(self):
        from django.db import transaction
        from django_logic.commands import _deferred_unlocks, note_deferred_unlock
        from django_logic.state import State

        inv = Invoice.objects.create(status='draft')
        conn = transaction.get_connection('default')
        _deferred_unlocks(conn).clear()

        # A transaction that rolls back: its clear hook is discarded.
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                note_deferred_unlock('default', State(inv, 'status'))
                raise RuntimeError('rollback')

        # Every later committing transaction must leave it empty.
        for _ in range(3):
            with transaction.atomic():
                note_deferred_unlock('default', State(inv, 'status'))
            self.assertEqual(len(_deferred_unlocks(conn)), 0)


class ReservedUserIdKwargTests(TestCase):
    def test_caller_supplied_user_id_is_dropped_loudly(self):
        """The worker popped ``user_id`` and replaced it with a live user, so the
        hook never saw the caller's value. Sync mode kept the value, so the
        difference only showed up in production."""
        from django_logic.background.serializers import serialize_kwargs

        with self.assertLogs('django-logic', level='WARNING') as logs:
            out = serialize_kwargs({'user_id': 'my-own-data', 'other': 1})
        self.assertNotIn('user_id', out)
        self.assertEqual(out['other'], 1)
        self.assertTrue(any('user_id' in line for line in logs.output))

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'STRICT_KWARGS_SERIALIZATION': True,
    })
    def test_strict_mode_raises_instead(self):
        from django_logic.background.serializers import (
            KwargsSerializationError, serialize_kwargs,
        )

        with self.assertRaises(KwargsSerializationError):
            serialize_kwargs({'user_id': 'my-own-data'})


class PublicSurfaceTests(TestCase):
    def test_star_import_exposes_only_the_documented_names(self):
        """Without __all__, `from django_logic import *` leaked whichever
        submodules happened to be imported, so the namespace varied with
        INSTALLED_APPS."""
        import django_logic

        self.assertEqual(sorted(django_logic.__all__), [
            'Action', 'Process', 'ProcessManager', 'Transition',
        ])
        for name in django_logic.__all__:
            self.assertTrue(hasattr(django_logic, name), name)
        # The command classes are not advertised, but a direct import from
        # django_logic.commands keeps working for existing consumers.
        from django_logic.commands import (  # noqa: F401
            Callbacks, Conditions, Permissions, SideEffects,
        )


class FailureBundleSwapTests(TestCase):
    def test_failure_bundle_is_swappable_like_the_other_four(self):
        """It was hardcoded, with no way to substitute it."""
        from django_logic.commands import Callbacks

        class LoudFailureCallbacks(Callbacks):
            pass

        class Custom(Transition):
            failure_callbacks_class = LoudFailureCallbacks

        t = Custom('go', sources=['draft'], target='approved')
        self.assertIsInstance(t.failure_callbacks, LoudFailureCallbacks)


