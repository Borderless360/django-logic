"""Pass-4 review fixes: the checks/settings/binding layer must not crash,
lie, or reject working topologies.

Each test here pins one confirmed finding:

* A bound ``BackgroundTransition`` without ``django_logic.background`` in
  ``INSTALLED_APPS`` was reported by nothing — every check early-returns on
  the missing app and the first ``.go()`` died with a raw missing-table
  error (``django_logic.E003``).
* ``DJANGO_LOGIC`` set to a non-dict raised a bare ``AttributeError`` out of
  ``ready()``, naming no setting.
* The #182 shadow validator checked every class in the nested tree, so a
  sibling process holding a helper named like another branch's transition
  rejected a working topology at import time.
* Unbinding was hand-rolled by every test that binds (registry
  comprehension + ``delattr``); ``ProcessManager.unbind_model_process`` is
  the supported inverse.
"""
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import (
    SimpleTestCase,
    TestCase,
    modify_settings,
    override_settings,
)

from django_logic.background.transitions import BackgroundTransition
from django_logic.checks import (
    check_background_app_is_installed,
    check_background_database_routing,
)
from django_logic.conf import validate_core_settings
from django_logic.process import (
    ModelProcessBinding,
    Process,
    ProcessManager,
)
from django_logic.transition import Transition
from tests import dl_settings
from tests.models import Invoice


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

# (The duck-typed-transition E001/sweep pins were retired with E001 and
# recover_stranded_states in 0.12.0 — nothing left reads failure attributes
# off arbitrary transition objects outside bind-time hook validation.)


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


# (InheritedColumnAmbiguityTests retired with collect_ambiguous_in_progress_states
# in 0.12.0 — see tests/test_binding_validation.py::SharedMarkerIsLegalTests for
# the replacement contract: marker sharing is legal, recovery is TM-scoped.)
