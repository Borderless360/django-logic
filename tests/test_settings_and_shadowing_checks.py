"""The checks/settings/binding layer must not crash, lie, or reject
working topologies.

Each test pins one confirmed finding:

* The install is one ``INSTALLED_APPS`` entry, ``'django_logic'``, and the
  app keeps the label ``django_logic_background`` — the address of the live
  table, its migration records and its content types. The retired second
  entry refuses to boot and names the fix. The pull-mode database and cache
  rules (``pull_mode_needs_postgresql``, ``pull_mode_needs_a_shared_cache``)
  fire only when a background
  transition is bound, so a sync-only install runs on SQLite with the
  default cache.
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
    check_background_database_routing,
    check_pull_mode_database,
    check_pull_mode_lock_cache,
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

# (The duck-typed-transition pins were retired with the ambiguous-recovery check and
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


class OneEntryInstallTests(_BindingHelper, SimpleTestCase):
    def test_the_app_keeps_the_label_of_the_live_table(self):
        config = apps.get_app_config('django_logic_background')
        self.assertEqual(config.name, 'django_logic')
        from django_logic.background.models import TransitionMessage
        self.assertEqual(TransitionMessage._meta.app_label,
                         'django_logic_background')
        self.assertEqual(TransitionMessage._meta.db_table,
                         'django_logic_background_transitionmessage')

    def test_the_migrations_live_under_the_installed_app(self):
        from django.db.migrations.loader import MigrationLoader
        loader = MigrationLoader(None, load=False)
        loader.load_disk()
        names = {name for app_label, name in loader.disk_migrations
                 if app_label == 'django_logic_background'}
        self.assertIn('0001_initial', names)
        self.assertIn('0010_proxy_model_label', names)

    def test_the_retired_second_entry_refuses_to_boot(self):
        from django.apps.config import AppConfig as DjangoAppConfig
        with self.assertRaises(ImproperlyConfigured) as ctx:
            DjangoAppConfig.create('django_logic.background')
        message = str(ctx.exception)
        self.assertIn("'django_logic'", message)
        self.assertIn('alone', message)


class RequestParamHookTests(_BindingHelper, SimpleTestCase):
    def test_a_hook_naming_request_fails_at_bind(self):
        def with_request(instance, request=None, **kwargs):
            pass

        class _RequestReadingProcess(Process):
            process_name = 'pass4_request_reader'
            transitions = [
                Transition('run', sources=['draft'], target='done',
                           side_effects=[with_request]),
            ]

        with self.assertRaises(ImproperlyConfigured) as ctx:
            self.bind(Invoice, _RequestReadingProcess)
        message = str(ctx.exception)
        self.assertIn('request', message)
        self.assertIn('with_request', message)


class MissingAppGuardTests(_BindingHelper, SimpleTestCase):
    @modify_settings(INSTALLED_APPS={'remove': 'django_logic'})
    def test_a_background_binding_without_the_app_is_refused(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self.bind(Invoice, _BackgroundBoundProcess)
        message = str(ctx.exception)
        self.assertIn('INSTALLED_APPS', message)
        self.assertIn('migrate', message)

    @modify_settings(INSTALLED_APPS={'remove': 'django_logic'})
    def test_a_sync_only_binding_without_the_app_still_binds(self):
        self.bind(Invoice, _SyncOnlyProcess)


class PullModeInfrastructureCheckTests(_BindingHelper, SimpleTestCase):
    _PULL = dict(dl_settings(), BACKGROUND_EXECUTION='pull')
    _SQLITE = {'default': {'ENGINE': 'django.db.backends.sqlite3',
                           'NAME': ':memory:'}}
    _LOCMEM = {'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

    def test_e004_names_sqlite_when_pull_mode_has_background_bindings(self):
        self.bind(Invoice, _BackgroundBoundProcess)
        with override_settings(DJANGO_LOGIC=self._PULL,
                               DATABASES=self._SQLITE):
            findings = check_pull_mode_database(None)
        self.assertEqual([f.id for f in findings], ['django_logic.pull_mode_needs_postgresql'])
        self.assertIn('SQLite', findings[0].msg)
        self.assertIn('PostgreSQL', findings[0].hint)

    def test_sqlite_is_fine_while_nothing_background_is_bound(self):
        saved = ProcessManager.bindings
        ProcessManager.bindings = [
            ModelProcessBinding(Invoice, _SyncOnlyProcess, 'status')]
        try:
            with override_settings(DJANGO_LOGIC=self._PULL,
                                   DATABASES=self._SQLITE):
                self.assertEqual(check_pull_mode_database(None), [])
        finally:
            ProcessManager.bindings = saved

    def test_e005_is_an_error_in_production_and_a_warning_with_debug(self):
        from django.core import checks as django_checks
        self.bind(Invoice, _BackgroundBoundProcess)
        with override_settings(DJANGO_LOGIC=self._PULL, CACHES=self._LOCMEM,
                               DEBUG=False):
            findings = check_pull_mode_lock_cache(None)
        self.assertEqual([f.id for f in findings], ['django_logic.pull_mode_needs_a_shared_cache'])
        self.assertIsInstance(findings[0], django_checks.Error)
        self.assertIn('per-process', findings[0].msg)
        with override_settings(DJANGO_LOGIC=self._PULL, CACHES=self._LOCMEM,
                               DEBUG=True):
            findings = check_pull_mode_lock_cache(None)
        self.assertEqual([f.id for f in findings], ['django_logic.pull_mode_needs_a_shared_cache'])
        self.assertIsInstance(findings[0], django_checks.Warning)


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
        from django_logic import conf as bg_settings

        with override_settings(DJANGO_LOGIC='BACKGROUND_EXECUTION=sync'):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                bg_settings.background_execution()
        self.assertIn('DJANGO_LOGIC must be a dict', str(ctx.exception))

    def test_ready_fails_with_the_setting_named(self):
        config = apps.get_app_config('django_logic_background')
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
# the replacement contract: marker sharing is legal, recovery works from that row.)
