"""Pass-4 review fixes: the checks/settings/binding layer must not crash,
lie, or reject working topologies.

Each test here pins one confirmed finding:

* ``_recovery_signature`` read the failure attributes off a transition
  unguarded, so a duck-typed transition took the E001 check (and the
  stranded sweep that calls it) down with an ``AttributeError``.
* A bound ``BackgroundTransition`` without ``django_logic.background`` in
  ``INSTALLED_APPS`` was reported by nothing — every check early-returns on
  the missing app and the first ``.go()`` died with a raw missing-table
  error (``django_logic.E003``).
* ``DJANGO_LOGIC`` set to a non-dict raised a bare ``AttributeError`` out of
  ``ready()``, naming no setting.
* ``TRANSITION_COVERAGE_LOG = True`` reached ``open()`` unvalidated, where a
  bool is a file descriptor: coverage lines went to stdout.
* The #182 shadow validator checked every class in the nested tree, so a
  sibling process holding a helper named like another branch's transition
  rejected a working topology at import time.
* Ambiguous-recovery collection keyed on the bound model's label, so an MTI
  child and its parent could claim one ``in_progress_state`` on the SAME
  inherited column with different recovery, unreported.
* Unbinding was hand-rolled by every test that binds (registry
  comprehension + ``delattr``); ``ProcessManager.unbind_model_process`` is
  the supported inverse.
"""
import pathlib

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import (
    SimpleTestCase,
    TestCase,
    modify_settings,
    override_settings,
)

from django_logic import coverage
from django_logic.background.transitions import BackgroundTransition
from django_logic.checks import (
    check_background_app_is_installed,
    check_background_database_routing,
    check_unambiguous_in_progress_ownership,
)
from django_logic.conf import validate_core_settings
from django_logic.process import (
    ModelProcessBinding,
    Process,
    ProcessManager,
    collect_ambiguous_in_progress_states,
    transition_observers,
)
from django_logic.transition import Transition
from tests import dl_settings
from tests.models import Invoice, MtiChild, MtiParent


class _BindingHelper:
    """Bind through here so every test unbinds itself — a leaked binding
    changes what the global collectors see for every later test."""

    def bind(self, model, process_class, state_field='status'):
        ProcessManager.bind_model_process(
            model, process_class, state_field=state_field)
        self.addCleanup(
            ProcessManager.unbind_model_process, model, process_class,
            state_field)


# --- A1: duck-typed transitions in the recovery signature -----------------

class _DuckTransition:
    """A custom transition object with the attributes the engine documents
    as required and none of the failure ones — the shape a consumer writes
    when its transition can only ever succeed or be retried."""

    def __init__(self, action_name, sources, target, in_progress_state):
        self.action_name = action_name
        self.sources = sources
        self.target = target
        self.in_progress_state = in_progress_state


class _DuckTypedProcess(Process):
    process_name = 'pass4_duck'
    transitions = [
        _DuckTransition('duck_run', sources=['draft'], target='done',
                        in_progress_state='duck_busy'),
    ]


class _RealClaimantProcess(Process):
    process_name = 'pass4_real_claimant'
    transitions = [
        Transition('real_run', sources=['draft'], target='done',
                   in_progress_state='duck_busy', failed_state='real_failed'),
    ]


class DuckTypedTransitionSignatureTests(_BindingHelper, SimpleTestCase):
    def test_collector_and_e001_survive_a_duck_typed_transition(self):
        self.bind(Invoice, _DuckTypedProcess)

        # No failed_state / failure_side_effects / failure_callbacks
        # anywhere on the transition, and neither the collector nor the
        # check may raise.
        self.assertNotIn(
            ('tests.Invoice', 'status', 'duck_busy'),
            collect_ambiguous_in_progress_states(),
        )
        self.assertEqual(
            [f for f in check_unambiguous_in_progress_ownership(None)
             if f.obj == 'tests.Invoice.status'],
            [],
        )

    def test_e001_still_fires_when_a_duck_claimant_shares_the_state(self):
        # Missing failure attributes read as "recovers with nothing", which
        # is a real disagreement with a claimant that has a failed_state.
        self.bind(Invoice, _DuckTypedProcess)
        self.bind(Invoice, _RealClaimantProcess)

        self.assertIn(
            ('tests.Invoice', 'status', 'duck_busy'),
            collect_ambiguous_in_progress_states(),
        )
        findings = [f for f in check_unambiguous_in_progress_ownership(None)
                    if f.obj == 'tests.Invoice.status']
        self.assertEqual([f.id for f in findings], ['django_logic.E001'])
        self.assertIn('_DuckTypedProcess.duck_run', findings[0].msg)
        self.assertIn('_RealClaimantProcess.real_run', findings[0].msg)


# --- A2: django_logic.E003, background app missing ------------------------

class _BackgroundBoundProcess(Process):
    process_name = 'pass4_background'
    transitions = [
        BackgroundTransition('bg_run', sources=['draft'], target='done',
                             in_progress_state='pass4_bg_busy',
                             failed_state='pass4_bg_failed'),
    ]


class _SyncOnlyProcess(Process):
    process_name = 'pass4_sync_only'
    transitions = [
        Transition('sync_run', sources=['draft'], target='done'),
    ]


class BackgroundAppInstalledCheckTests(_BindingHelper, SimpleTestCase):
    def test_no_finding_while_the_app_is_installed(self):
        self.bind(Invoice, _BackgroundBoundProcess)
        self.assertEqual(check_background_app_is_installed(None), [])

    @modify_settings(INSTALLED_APPS={'remove': 'django_logic.background'})
    def test_e003_when_a_background_transition_is_bound_without_the_app(self):
        self.bind(Invoice, _BackgroundBoundProcess)

        findings = check_background_app_is_installed(None)

        self.assertEqual([f.id for f in findings], ['django_logic.E003'])
        self.assertIn('tests.Invoice', findings[0].msg)
        self.assertIn('INSTALLED_APPS', findings[0].msg)
        self.assertIn('migrate', findings[0].hint)

    @modify_settings(INSTALLED_APPS={'remove': 'django_logic.background'})
    def test_silent_without_the_app_when_nothing_background_is_bound(self):
        # A sync-only install must not be told to add an app it has no use
        # for. Only this binding is visible for the duration.
        saved = ProcessManager.bindings
        ProcessManager.bindings = [
            ModelProcessBinding(Invoice, _SyncOnlyProcess, 'status')]
        try:
            self.assertEqual(check_background_app_is_installed(None), [])
        finally:
            ProcessManager.bindings = saved


# --- A3: DJANGO_LOGIC is not a dict --------------------------------------

class NonDictSettingsBlockTests(SimpleTestCase):
    def test_core_validation_names_the_setting(self):
        # Including the falsy non-dicts: '' used to read as "unset", so a
        # DJANGO_LOGIC built from an empty env var was silently ignored.
        for bad in ('LOCK_TIMEOUT=7200', ['LOCK_TIMEOUT'], 7200, '', []):
            with self.subTest(value=bad):
                with override_settings(DJANGO_LOGIC=bad):
                    with self.assertRaises(ImproperlyConfigured) as ctx:
                        validate_core_settings()
                self.assertIn('DJANGO_LOGIC must be a dict', str(ctx.exception))
                self.assertIn(type(bad).__name__, str(ctx.exception))

    def test_background_readers_name_the_setting(self):
        from django_logic.background import settings as bg_settings

        with override_settings(DJANGO_LOGIC='BACKGROUND_EXECUTION=sync'):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                bg_settings.background_execution()
        self.assertIn('DJANGO_LOGIC must be a dict', str(ctx.exception))

    def test_both_ready_hooks_fail_with_the_setting_named(self):
        for label in ('django_logic', 'django_logic_background'):
            with self.subTest(app=label):
                config = apps.get_app_config(label)
                with override_settings(DJANGO_LOGIC='not-a-dict'):
                    with self.assertRaises(ImproperlyConfigured) as ctx:
                        config.ready()
                self.assertIn('DJANGO_LOGIC must be a dict', str(ctx.exception))
                # A healthy configuration keeps ready() a no-op re-run.
                config.ready()

    def test_unset_and_empty_are_still_the_documented_default(self):
        for value in (None, {}):
            with self.subTest(value=value):
                with override_settings(DJANGO_LOGIC=value):
                    validate_core_settings()


# --- A4: TRANSITION_COVERAGE_LOG is a path, not a file descriptor --------

class CoverageLogValidationTests(SimpleTestCase):
    def test_true_is_refused_instead_of_writing_to_stdout(self):
        # open(True) writes to file descriptor 1, so an un-validated True
        # appended coverage lines to stdout for the rest of the process.
        with override_settings(
                DJANGO_LOGIC=dl_settings(TRANSITION_COVERAGE_LOG=True)):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                validate_core_settings()
        self.assertIn('TRANSITION_COVERAGE_LOG', str(ctx.exception))

    def test_non_path_values_are_refused(self):
        for bad in (True, 1, ['/tmp/coverage.log'], {'path': '/tmp/x'}):
            with self.subTest(value=bad):
                with override_settings(
                        DJANGO_LOGIC=dl_settings(TRANSITION_COVERAGE_LOG=bad)):
                    with self.assertRaises(ImproperlyConfigured) as ctx:
                        validate_core_settings()
                self.assertIn(
                    'TRANSITION_COVERAGE_LOG', str(ctx.exception))

    def test_paths_and_none_are_accepted(self):
        for good in (None, '/tmp/pass4_coverage.log',
                     pathlib.Path('/tmp/pass4_coverage.log')):
            with self.subTest(value=good):
                with override_settings(
                        DJANGO_LOGIC=dl_settings(TRANSITION_COVERAGE_LOG=good)):
                    validate_core_settings()

    def test_ready_refuses_at_boot_and_records_nothing(self):
        # Reverting the validation makes ready() install a recorder writing
        # to fd 1; drop it either way so no later test logs to stdout.
        self.addCleanup(coverage.stop_file_recording)
        config = apps.get_app_config('django_logic')
        observers_before = list(transition_observers)

        with override_settings(
                DJANGO_LOGIC=dl_settings(TRANSITION_COVERAGE_LOG=True)):
            with self.assertRaises(ImproperlyConfigured):
                config.ready()

        self.assertEqual(list(transition_observers), observers_before)
        config.ready()


# --- F1: only the ROOT's attributes can shadow an action_name -----------

class _ShadowSiblingWithTransition(Process):
    process_name = 'pass4_shadow_sibling_a'
    transitions = [
        Transition('helper_action', sources=['draft'], target='done'),
    ]


class _ShadowSiblingWithHelper(Process):
    """A nested process that happens to own a helper named like the OTHER
    branch's transition. Dispatch never looks this class up by attribute."""
    process_name = 'pass4_shadow_sibling_b'

    @staticmethod
    def helper_action(value):
        return value


class ActionNameShadowingTests(_BindingHelper, TestCase):
    def test_sibling_helper_topology_binds_and_dispatches(self):
        # Defined here, not at module level: the over-broad validator raised
        # at class creation, and a failing import would hide every other pin
        # in this module.
        root_process = type('_ShadowRootProcess', (Process,), {
            'process_name': 'pass4_shadow_root',
            'nested_processes': [_ShadowSiblingWithTransition,
                                 _ShadowSiblingWithHelper],
        })
        self.bind(Invoice, root_process)
        invoice = Invoice.objects.create(status='draft')

        invoice.pass4_shadow_root.helper_action()

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'done')

    def test_root_attribute_shadowing_a_nested_action_still_raises(self):
        class _Nested(Process):
            process_name = 'pass4_shadow_nested'
            transitions = [
                Transition('nested_action', sources=['draft'], target='done'),
            ]

        with self.assertRaises(ImproperlyConfigured) as ctx:
            type('_ShadowingRoot', (Process,), {
                'process_name': 'pass4_shadowing_root',
                'nested_processes': [_Nested],
                'nested_action': staticmethod(lambda: None),
            })
        self.assertIn('nested_action', str(ctx.exception))

    def test_root_attribute_shadowing_its_own_action_still_raises(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            type('_SelfShadowingRoot', (Process,), {
                'process_name': 'pass4_self_shadowing_root',
                'transitions': [
                    Transition('is_valid', sources=['draft'], target='done'),
                ],
            })
        self.assertIn('is_valid', str(ctx.exception))


# --- F3-lite: ambiguity follows the physical column ----------------------

class _MtiParentProcess(Process):
    process_name = 'pass4_mti_parent'
    transitions = [
        Transition('parent_run', sources=['draft'], target='done',
                   in_progress_state='mti_busy',
                   failed_state='parent_failed'),
    ]


class _MtiChildProcess(Process):
    process_name = 'pass4_mti_child'
    transitions = [
        Transition('child_run', sources=['draft'], target='done',
                   in_progress_state='mti_busy',
                   failed_state='child_failed'),
    ]


class _MtiSharedProcess(Process):
    """One class bound to both models — the supported MTI shape, where each
    model drives its own copy of the same machine."""
    process_name = 'pass4_mti_shared'
    transitions = [
        Transition('shared_run', sources=['draft'], target='done',
                   in_progress_state='mti_shared_busy',
                   failed_state='shared_failed'),
    ]


class InheritedColumnAmbiguityTests(_BindingHelper, SimpleTestCase):
    PARENT_KEY = ('tests.MtiParent', 'status', 'mti_busy')
    CHILD_KEY = ('tests.MtiChild', 'status', 'mti_busy')

    def test_shared_state_on_an_inherited_column_is_ambiguous(self):
        # MtiChild.status has no column of its own: both processes write
        # tests_mtiparent.status with different recovery.
        self.bind(MtiParent, _MtiParentProcess)
        self.bind(MtiChild, _MtiChildProcess)

        ambiguous = collect_ambiguous_in_progress_states()
        self.assertIn(self.PARENT_KEY, ambiguous)
        # Keyed by the bound model too, so the stranded sweep — which looks
        # itself up by its own binding — still skips it.
        self.assertIn(self.CHILD_KEY, ambiguous)
        findings = check_unambiguous_in_progress_ownership(None)
        objs = {f.obj for f in findings if f.id == 'django_logic.E001'}
        self.assertIn('tests.MtiParent.status', objs)
        self.assertIn('tests.MtiChild.status', objs)
        claimants = [f.msg for f in findings
                     if f.obj == 'tests.MtiChild.status']
        self.assertIn('_MtiParentProcess.parent_run', claimants[0])
        self.assertIn('_MtiChildProcess.child_run', claimants[0])

    def test_distinct_columns_on_the_same_mti_pair_are_fine(self):
        # 'extra' is the child's OWN column, so nothing is shared.
        self.bind(MtiParent, _MtiParentProcess)
        self.bind(MtiChild, _MtiChildProcess, state_field='extra')

        ambiguous = collect_ambiguous_in_progress_states()
        self.assertNotIn(self.PARENT_KEY, ambiguous)
        self.assertNotIn(('tests.MtiChild', 'extra', 'mti_busy'), ambiguous)
        self.assertEqual(
            [f for f in check_unambiguous_in_progress_ownership(None)
             if f.obj.startswith('tests.Mti')],
            [],
        )

    def test_one_process_bound_to_both_mti_models_is_not_flagged(self):
        # Same class, same recovery signature: sharing the inherited column
        # is exactly what this topology means.
        self.bind(MtiParent, _MtiSharedProcess)
        self.bind(MtiChild, _MtiSharedProcess)

        ambiguous = collect_ambiguous_in_progress_states()
        self.assertNotIn(
            ('tests.MtiParent', 'status', 'mti_shared_busy'), ambiguous)
        self.assertNotIn(
            ('tests.MtiChild', 'status', 'mti_shared_busy'), ambiguous)


# --- B-REG: unbind_model_process -----------------------------------------

class _UnbindProcess(Process):
    process_name = 'pass4_unbind'
    transitions = [
        Transition('run', sources=['draft'], target='done'),
    ]


class _SecondFieldProcess(Process):
    process_name = 'pass4_unbind_second'
    transitions = [
        Transition('flag', sources=['draft'], target='done'),
    ]


class UnbindModelProcessTests(SimpleTestCase):
    def tearDown(self):
        for process_class in (_UnbindProcess, _SecondFieldProcess):
            ProcessManager.unbind_model_process(Invoice, process_class)
        super().tearDown()

    def _bindings_for(self, process_class):
        return [b for b in ProcessManager.bindings
                if b.process_class is process_class]

    def test_bind_unbind_rebind_leaves_no_residue(self):
        ProcessManager.bind_model_process(
            Invoice, _UnbindProcess, state_field='status')
        self.assertTrue(self._bindings_for(_UnbindProcess))
        self.assertIsInstance(
            Invoice(status='draft').pass4_unbind, _UnbindProcess)

        ProcessManager.unbind_model_process(Invoice, _UnbindProcess)

        self.assertEqual(self._bindings_for(_UnbindProcess), [])
        self.assertNotIn('pass4_unbind', vars(Invoice))
        self.assertFalse(hasattr(Invoice(status='draft'), 'pass4_unbind'))

        # Rebinding is a fresh bind, not a duplicate-name rejection.
        ProcessManager.bind_model_process(
            Invoice, _UnbindProcess, state_field='status')
        self.assertEqual(len(self._bindings_for(_UnbindProcess)), 1)
        self.assertIsInstance(
            Invoice(status='draft').pass4_unbind, _UnbindProcess)

    def test_unbinding_something_unbound_is_a_no_op(self):
        before = list(ProcessManager.bindings)
        ProcessManager.unbind_model_process(Invoice, _UnbindProcess)
        self.assertEqual(ProcessManager.bindings, before)

    def test_unbinding_one_process_keeps_the_models_other_machines(self):
        ProcessManager.bind_model_process(
            Invoice, _UnbindProcess, state_field='status')
        ProcessManager.bind_model_process(
            Invoice, _SecondFieldProcess, state_field='customer_received')

        ProcessManager.unbind_model_process(Invoice, _UnbindProcess)

        self.assertNotIn('pass4_unbind', vars(Invoice))
        self.assertIn('pass4_unbind_second', vars(Invoice))
        self.assertEqual(len(self._bindings_for(_SecondFieldProcess)), 1)

    def test_state_field_narrows_which_binding_is_removed(self):
        ProcessManager.bind_model_process(
            Invoice, _UnbindProcess, state_field='status')

        ProcessManager.unbind_model_process(
            Invoice, _UnbindProcess, state_field='customer_received')
        self.assertEqual(len(self._bindings_for(_UnbindProcess)), 1)

        ProcessManager.unbind_model_process(
            Invoice, _UnbindProcess, state_field='status')
        self.assertEqual(self._bindings_for(_UnbindProcess), [])


# --- docstring honesty ---------------------------------------------------

class RouterCheckDocstringTests(SimpleTestCase):
    def test_e002_admits_hint_dependent_routers_can_pass(self):
        # The check asks the router about model CLASSES; a router that
        # decides from hints can answer 'default' here and still split a
        # real write. The docstring must not claim otherwise.
        doc = check_background_database_routing.__doc__
        self.assertIn('hints', doc)
        self.assertIn('not proof', doc)
