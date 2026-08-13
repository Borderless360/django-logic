"""Regression tests for the defects found reviewing 0.11.0 (#178-#182).

Each test states the wrong behaviour it pins shut, so a future refactor that
reintroduces it fails here rather than in a consumer's production data.
"""
from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.db.models.signals import pre_save
from django.template import Context
from django.template.engine import Engine
from django.test import TestCase, TransactionTestCase, override_settings

from django_logic import Process, ProcessManager, Transition
from django_logic.background import BackgroundTransition
from django_logic.background.models import TransitionMessage
from django_logic.background.settings import (
    _validate_bool,
    strict_kwargs_serialization,
)
from django_logic.background.tasks import (
    retry_stale_transitions,
    watchdog_stale_attempts,
)
from django_logic.checks import check_no_unknown_settings
from django_logic.logger import TransitionEventType
from tests.models import Invoice, MtiChild, MtiParent


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


# --- #178: a failing state write must be accounted ----------------------

def _write_a_sibling_row(instance, **kwargs):
    """A side-effect with an observable database write, so the attempt
    savepoint's rollback can actually be asserted (a ``_noop`` side-effect
    leaves nothing to roll back, which made this pin vacuous)."""
    Invoice.objects.create(status='sibling')


class RejectedTargetWriteProcess(Process):
    process_name = 'rejected_target_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='rejected_target',
            in_progress_state='rt_running', failed_state='rt_failed',
            side_effects=[_write_a_sibling_row],
        ),
    ]


class RejectedStateWriteTests(_BindCleanup, TestCase):
    """#178 — a state write the database refuses used to escape the outer
    atomic, rolling back record_error with it: errors_count stayed 0, the
    starter re-dispatched forever, and the side-effects re-ran forever."""

    _bound = (RejectedTargetWriteProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, RejectedTargetWriteProcess, state_field='status')
        cache.clear()

        def veto(sender, instance, **kwargs):
            if instance.status == 'rejected_target':
                raise ValueError('the database refuses this state')

        self._veto = veto
        pre_save.connect(veto, sender=Invoice)
        self.addCleanup(pre_save.disconnect, veto, sender=Invoice)

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERRORS': 3,
        'TRANSITION_MESSAGE_RETRY_MINUTES': 0,
    })
    def test_rejected_target_write_is_charged_and_terminates(self):
        inv = Invoice.objects.create(status='draft')
        with self.assertRaises(ValueError):
            inv.rejected_target_proc.go()

        tm = TransitionMessage.objects.get(instance_id=str(inv.pk))
        # The attempt was charged — this is the whole fix. It used to be 0.
        self.assertEqual(tm.errors_count, 1)
        self.assertFalse(tm.is_completed)

        # And the retry loop terminates instead of running forever.
        for _ in range(5):
            try:
                retry_stale_transitions()
            except ValueError:
                pass
        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.errors_count, 3)

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERRORS': 3,
        'TRANSITION_MESSAGE_RETRY_MINUTES': 0,
    })
    def test_target_write_rolls_back_its_own_attempt(self):
        """The write lives inside the attempt savepoint, so a rejected write
        leaves no partial state behind.

        The side-effect's own row is the witness: asserting only on the
        instance's state proved nothing, because the veto blocks the target
        write whether or not a savepoint contains it (caught by a mutation
        that moved the write back out of the savepoint and still passed).
        """
        inv = Invoice.objects.create(status='draft')
        with self.assertRaises(ValueError):
            inv.rejected_target_proc.go()
        inv.refresh_from_db()
        # Never the target; still the in-progress state phase 1 wrote.
        self.assertEqual(inv.status, 'rt_running')
        # The attempt was all-or-nothing: the side-effect's write is gone.
        self.assertFalse(
            Invoice.objects.filter(status='sibling').exists(),
            'the failed attempt left a side-effect write behind — the target '
            'write is not inside the attempt savepoint',
        )


# --- #179: the timeout watchdog --------------------------------------------

def _raise_slow(instance, **kwargs):
    raise ValueError('slow boom')


def _die(instance, **kwargs):
    raise SystemExit('worker killed mid-attempt')


class WatchdogChargeProcess(Process):
    process_name = 'wd_charge_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='wd_done',
            in_progress_state='wd_running', failed_state='wd_failed',
            timeout=1, side_effects=[_raise_slow],
        ),
    ]


class WatchdogCrashProcess(Process):
    process_name = 'wd_crash_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='wd2_done',
            in_progress_state='wd2_running', failed_state='wd2_failed',
            timeout=1, side_effects=[_die],
        ),
    ]


@override_settings(DJANGO_LOGIC={
    'BACKGROUND_EXECUTION': 'sync',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
})
class WatchdogAccountingTests(_BindCleanup, TestCase):
    _bound = (WatchdogChargeProcess, WatchdogCrashProcess)

    def setUp(self):
        super().setUp()
        cache.clear()

    def _age_attempt(self, tm):
        """Push started_at past the timeout without sleeping."""
        from datetime import timedelta
        from django.utils import timezone
        TransitionMessage.objects.filter(pk=tm.pk).update(
            started_at=timezone.now() - timedelta(seconds=30))
        tm.refresh_from_db()

    def test_attempt_that_recorded_its_own_error_is_not_charged_again(self):
        """#179 — three ticks with no new attempts used to take errors_count
        from 1 to 4, silently spending the consumer's retries and burying the
        real error message under a synthetic timeout."""
        ProcessManager.bind_model_process(
            Invoice, WatchdogChargeProcess, state_field='status')
        inv = Invoice.objects.create(status='draft')
        with self.assertRaises(ValueError):
            inv.wd_charge_proc.go()

        tm = TransitionMessage.objects.get(instance_id=str(inv.pk))
        self.assertEqual(tm.errors_count, 1)
        self._age_attempt(tm)

        for _ in range(3):
            self.assertEqual(watchdog_stale_attempts(), 0)
        tm.refresh_from_db()
        self.assertEqual(tm.errors_count, 1)
        # The real cause survives.
        self.assertEqual(tm.last_error_message, 'slow boom')

    def test_abandoned_attempt_is_visible_and_charged_exactly_once(self):
        """#179 — started_at is committed before the attempt and survives it
        rolling back, so a worker that dies mid-flight is observable. Before
        the fix the marker vanished with the transaction and timeout= could
        never fire at all."""
        ProcessManager.bind_model_process(
            Invoice, WatchdogCrashProcess, state_field='status')
        inv = Invoice.objects.create(status='draft')
        with self.assertRaises(SystemExit):
            inv.wd_crash_proc.go()

        tm = TransitionMessage.objects.get(instance_id=str(inv.pk))
        self.assertIsNotNone(tm.started_at)   # survived the rollback
        self.assertEqual(tm.errors_count, 0)  # the attempt recorded nothing
        self._age_attempt(tm)

        self.assertEqual(watchdog_stale_attempts(), 1)
        tm.refresh_from_db()
        self.assertEqual(tm.errors_count, 1)
        # Charged once, not once per tick.
        self.assertEqual(watchdog_stale_attempts(), 0)
        tm.refresh_from_db()
        self.assertEqual(tm.errors_count, 1)


# --- #180: nested-process tree walk ---------------------------------------

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
        """#180 — a leaf reachable by two paths yielded its transitions twice,
        so resolution rejected the single declaration as 'several transitions
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
        """The copy-paste case: the same class listed twice."""
        ProcessManager.bind_model_process(
            Invoice, DuplicateNestedProcess, state_field='status')
        inv = Invoice.objects.create(status='draft')
        inv.dup_nested.leaf_act()
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'approved')

    def test_nested_cycle_does_not_recurse_forever(self):
        """#180 — A -> B -> A used to die with RecursionError."""
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


# --- #181: templates must not drive the state machine ---------------------

class TemplateSafeProcess(Process):
    process_name = 'tpl_safe_proc'
    transitions = [
        Transition('approve', sources=['draft'], target='approved'),
    ]


class TemplateRenderTests(_BindCleanup, TestCase):
    _bound = (TemplateSafeProcess,)

    def test_rendering_a_transition_does_not_execute_it(self):
        """#181 — Django calls any callable a template resolves, so
        ``{{ obj.process.approve }}`` transitioned the object while rendering
        a page (and printed the tr_id into the output)."""
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


# --- #182: definitions the engine must refuse ----------------------------

class ShadowedDefinitionTests(TestCase):
    def test_action_name_shadowed_by_process_attribute_is_rejected(self):
        """#182 — such a transition was advertised and silently did nothing,
        because __getattr__ only runs when attribute lookup fails."""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            class Shadowed(Process):
                process_name = 'shadowed_proc'
                transitions = [
                    Transition('is_valid', sources=['draft'], target='approved'),
                ]

        self.assertIn('is_valid', str(ctx.exception))
        self.assertIn('shadowed', str(ctx.exception).lower())

    def test_process_name_colliding_with_model_field_is_rejected(self):
        """#182 — binding replaced the field's descriptor with a read-only
        property, after which the model could not be instantiated at all."""
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
        """#182 — it was bool()-coerced, so the string 'false' (an env var
        read straight through) switched strict mode ON."""
        self.assertFalse(strict_kwargs_serialization())
        with self.assertRaises(ImproperlyConfigured):
            _validate_bool('STRICT_KWARGS_SERIALIZATION')

    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'STRICT_KWARGS_SERIALIZATION': True,
    })
    def test_literal_true_enables_strict_mode(self):
        self.assertTrue(strict_kwargs_serialization())
        _validate_bool('STRICT_KWARGS_SERIALIZATION')   # must not raise


class UnknownSettingsCheckTests(TestCase):
    @override_settings(DJANGO_LOGIC={
        'BACKGROUND_EXECUTION': 'sync',
        'TRANSITION_MESSAGE_MAX_ERROR': 3,      # typo: missing S
    })
    def test_typo_is_reported_as_w004(self):
        """#182 — a misspelled key was silently ignored and the default
        silently applied."""
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


# --- the low-severity sweep -----------------------------------------------

class BareStringSourcesTests(TestCase):
    def test_sources_as_a_bare_string_is_rejected(self):
        """`list('draft')` is ['d','r','a','f','t'], which matches no state:
        the transition became invisible to get_available_actions() and calling
        it reported a missing action rather than a bad declaration."""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            Transition('go', sources='draft', target='approved')
        self.assertIn('iterated per character', str(ctx.exception))

    def test_a_list_of_one_state_is_still_fine(self):
        t = Transition('go', sources=['draft'], target='approved')
        self.assertEqual(t.sources, ['draft'])


class DeferredUnlockRegistryTests(TransactionTestCase):
    """The registry registered its on_commit clear only while it was empty. A
    rollback discards the hook but keeps the entries, so it was never empty
    again and never cleared — growing for the life of the connection."""

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

        # Every subsequent committing transaction must leave it empty.
        for _ in range(3):
            with transaction.atomic():
                note_deferred_unlock('default', State(inv, 'status'))
            self.assertEqual(len(_deferred_unlocks(conn)), 0)


class ReservedUserIdKwargTests(TestCase):
    def test_caller_supplied_user_id_is_dropped_loudly(self):
        """restore_user popped it in phase 2 and replaced it with a live user,
        so the hook never saw the caller's value — and the same call behaved
        correctly in sync mode, a parity break only visible in production."""
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
            'Action', 'Callbacks', 'Conditions',
            'Permissions', 'Process', 'ProcessManager', 'SideEffects',
            'Transition',
        ])
        for name in django_logic.__all__:
            self.assertTrue(hasattr(django_logic, name), name)


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


# --- self-review of the 0.12.0 fixes themselves ---------------------------

class RejectedFailedStateWriteProcess(Process):
    """A transition whose failed_state the database refuses."""
    process_name = 'rej_failed_proc'
    transitions = [
        Transition('go', sources=['draft'], target='rf_done',
                   failed_state='rf_refused',
                   side_effects=[_raise_slow]),
    ]


class FailedStateWriteHonestyTests(_BindCleanup, TestCase):
    """The savepoints added for #178 must not lie about what they wrote."""

    _bound = (RejectedFailedStateWriteProcess,)

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, RejectedFailedStateWriteProcess, state_field='status')
        cache.clear()

        def veto(sender, instance, **kwargs):
            if instance.status == 'rf_refused':
                raise ValueError('the database refuses failed_state')

        pre_save.connect(veto, sender=Invoice)
        self.addCleanup(pre_save.disconnect, veto, sender=Invoice)

    def test_a_rejected_failed_state_write_does_not_log_set_state(self):
        """The SET_STATE line is the state-change record the trace and
        log-based assertions read; emitting it for a write that never landed
        would be a false entry. (Caught reviewing 0.12.0's own diff.)"""
        inv = Invoice.objects.create(status='draft')
        with self.assertLogs('django-logic', level='INFO') as logs:
            with self.assertRaises(ValueError) as ctx:
                inv.rej_failed_proc.go()

        # The ORIGINAL failure propagates, not the write's own exception.
        self.assertEqual(str(ctx.exception), 'slow boom')
        # TransitionEventType.SET_STATE.value, not a hand-typed 'Set state':
        # the engine logs 'Set State', so the lowercase literal matched
        # nothing and this assertion passed against an empty list however
        # false the log was (caught by Cursor Bugbot on this very PR — the
        # same "test that proves nothing" shape as the fake MTI test).
        set_state_lines = [
            line for line in logs.output
            if 'rf_refused' in line and TransitionEventType.SET_STATE.value in line
        ]
        self.assertEqual(set_state_lines, [], f'false SET_STATE: {set_state_lines}')
        # And the failure was reported.
        self.assertTrue(any('could not write failed_state' in line
                            for line in logs.output))


class FailureErrorAccumulationTests(TestCase):
    """record_failure_side_effect_error must not erase an earlier note.

    Overwriting meant whichever note came second silently erased the
    other. (Caught reviewing 0.12.0's own diff.)
    """

    def test_two_recorded_problems_both_survive(self):
        tm = TransitionMessage.objects.create(
            app_label='tests', model_name='invoice', instance_id='1',
            process_name='acc_proc', transition_name='go', queue_name='q')

        tm.record_failure_side_effect_error(
            ValueError('write refused'), label='failed_state write')
        tm.record_failure_side_effect_error(
            RuntimeError('cleanup broke'), label='failed_state write')

        tm.refresh_from_db()
        self.assertIn('failed_state write: ValueError: write refused',
                      tm.failure_side_effect_error)
        self.assertIn('failed_state write: RuntimeError: cleanup broke',
                      tm.failure_side_effect_error)


# (StrandedRecoveryHonestyTests retired in 0.12.0 with recover_stranded_states
# itself — in_progress_state is background-only now, so no record-less
# stranding exists for a sweep to recover or misreport.)


class ShadowValidatorInstanceAttrTests(TestCase):
    def test_an_action_named_state_is_rejected(self):
        """`state` is set on the INSTANCE by Process.__init__, so
        hasattr(cls, 'state') is False and the class-only check accepted it —
        while at runtime the transition was unreachable. It is also the first
        example the validator's own docstring cites."""
        for name in ('state', 'instance', 'field_name'):
            with self.subTest(action_name=name):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    type(f'Shadow_{name}', (Process,), {
                        'process_name': f'shadow_{name}_proc',
                        'transitions': [
                            Transition(name, sources=['draft'], target='approved'),
                        ],
                    })
                self.assertIn(name, str(ctx.exception))


class MtiBindingTests(TestCase):
    """A multi-table-inheritance child may bind the same process_name as its
    parent: setattr installs the child's OWN accessor, shadowing the parent's,
    and each model drives its own process. The MRO collision check rejected
    that working shape until it learned to ignore accessors django-logic
    itself installed.

    Caught reviewing the regression-fix commits — and the first version of
    this test was a fake: it asserted an isinstance() on a NON-MTI model and
    passed with the fix removed. It now binds a real parent/child pair and
    drives the child, so removing the fix fails it.
    """

    _procs: tuple = ()

    def tearDown(self):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings if b.process_class not in self._procs
        ]
        for model in (MtiParent, MtiChild):
            for proc in self._procs:
                if proc.process_name in vars(model):
                    delattr(model, proc.process_name)
        super().tearDown()

    def test_child_may_reuse_the_parents_process_name(self):
        class ParentFlow(Process):
            process_name = 'mti_flow'
            transitions = [
                Transition('go', sources=['draft'], target='parent_done'),
            ]

        class ChildFlow(Process):
            process_name = 'mti_flow'          # same name, MTI child
            transitions = [
                Transition('go', sources=['draft'], target='child_done'),
            ]

        self._procs = (ParentFlow, ChildFlow)
        ProcessManager.bind_model_process(
            MtiParent, ParentFlow, state_field='status')
        # This is the call that raised before the fix.
        ProcessManager.bind_model_process(
            MtiChild, ChildFlow, state_field='status')

        cache.clear()
        child = MtiChild.objects.create(status='draft')
        child.mti_flow.go()
        child.refresh_from_db()
        # The child ran ITS process, not the parent's.
        self.assertEqual(child.status, 'child_done')

        parent = MtiParent.objects.create(status='draft')
        parent.mti_flow.go()
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'parent_done')

    def test_a_real_attribute_clash_is_still_rejected(self):
        class Clashing(Process):
            process_name = 'save'          # Model.save lives on the MRO
            transitions = [
                Transition('go', sources=['draft'], target='approved'),
            ]

        self._procs = (Clashing,)
        with self.assertRaises(ImproperlyConfigured) as ctx:
            ProcessManager.bind_model_process(
                Invoice, Clashing, state_field='status')
        self.assertIn('already names something', str(ctx.exception))

    def test_a_model_method_named_like_the_process_is_rejected(self):
        class HasMethod(Process):
            process_name = 'clashing_method'
            transitions = [
                Transition('go', sources=['draft'], target='approved'),
            ]

        self._procs = (HasMethod,)
        Invoice.clashing_method = lambda self: 'business logic'
        self.addCleanup(lambda: delattr(Invoice, 'clashing_method'))
        with self.assertRaises(ImproperlyConfigured) as ctx:
            ProcessManager.bind_model_process(
                Invoice, HasMethod, state_field='status')
        self.assertIn('already names something', str(ctx.exception))
