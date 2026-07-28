"""Regression tests for the defects found reviewing 0.11.0 (#178-#182).

Each test states the wrong behaviour it pins shut, so a future refactor that
reintroduces it fails here rather than in a consumer's production data.
"""
from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.db.models.signals import pre_save
from django.template import Context
from django.template.engine import Engine
from django.test import TestCase, override_settings

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


# --- #178: a failing state write must be accounted ----------------------

class RejectedTargetWriteProcess(Process):
    process_name = 'rejected_target_proc'
    transitions = [
        BackgroundTransition(
            'go', sources=['draft'], target='rejected_target',
            in_progress_state='rt_running', failed_state='rt_failed',
            side_effects=[_noop],
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
        leaves no partial state behind."""
        inv = Invoice.objects.create(status='draft')
        with self.assertRaises(ValueError):
            inv.rejected_target_proc.go()
        inv.refresh_from_db()
        # Never the target; still the in-progress state phase 1 wrote.
        self.assertEqual(inv.status, 'rt_running')


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
        self.assertIn('collides with a field', str(ctx.exception))


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
    def test_removed_keys_are_left_to_w003(self):
        """W003 already names removed keys with migration advice; W004 must
        not duplicate the report."""
        self.assertEqual(check_no_unknown_settings(None), [])
