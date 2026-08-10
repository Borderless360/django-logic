"""``DJANGO_LOGIC['LEGACY_EXCEPTION_BASE']`` — fork coexistence (#190).

A consumer migrating off a differently-named fork runs both engines side
by side, with shared handlers that catch the *fork's*
``TransitionNotAllowed``. The setting mixes the fork's class into this
engine's ``TransitionNotAllowed`` at ``ready()`` so those handlers keep
answering gracefully; every failure mode must raise
``ImproperlyConfigured`` at boot — a broken bridge must never be silent.
"""
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from django_logic import Process, ProcessManager, Transition
from django_logic.background.exceptions import (
    AlreadyInProgress,
    SourceStateChanged,
)
from django_logic.checks import check_no_unknown_settings
from django_logic.conf import (
    install_legacy_exception_base,
    validate_core_settings,
)
from django_logic.exceptions import (
    TransitionNotAllowed,
    TransitionTemporarilyUnavailable,
)
from tests import dl_settings
from tests.models import Invoice


def _conf(**overrides):
    return dl_settings(**overrides)


class LegacyTransitionNotAllowed(Exception):
    """Stands in for the fork's exception class, importable by dotted path."""


class NotAnException:
    pass


class CycleBase(TransitionNotAllowed):
    """Mixing a SUBCLASS back into TransitionNotAllowed's bases is an
    inheritance cycle — the TypeError path the installer must translate."""


class ArgfulLegacyBase(Exception):
    """A fork base with a non-message constructor. Neither
    TransitionNotAllowed nor DjangoLogicException defines ``__init__``, so
    through the new MRO this would service every denial's construction —
    booting green and then raising TypeError at every raise site."""

    def __init__(self, code, message):
        super().__init__(f'{code}: {message}')


class MessageEatingLegacyBase(Exception):
    """The common fork idiom that constructs fine but blanks ``str(exc)``
    and ``args`` for every denial — and ``args=()`` breaks exception
    (un)pickling wherever celery/tblib serialize exception info (#196)."""

    def __init__(self, message):
        self.message = message
        super().__init__()


class SystemExitingLegacyBase(Exception):
    """A fork ``__init__`` that raises a BaseException during boot. The
    installer's unwind must still run (#196)."""

    def __init__(self, *args):
        raise SystemExit(3)


NOT_A_CLASS = object()

LEGACY_PATH = 'tests.test_legacy_exception_base.LegacyTransitionNotAllowed'


class _BasesCleanup:
    """``__bases__`` mutation is process-global — restore it or the bridge
    leaks into every other test in the run."""

    def setUp(self):
        super().setUp()
        self._saved_bases = TransitionNotAllowed.__bases__

    def tearDown(self):
        TransitionNotAllowed.__bases__ = self._saved_bases
        super().tearDown()


class BridgeInstallTests(_BasesCleanup, SimpleTestCase):
    @override_settings(DJANGO_LOGIC=_conf(LEGACY_EXCEPTION_BASE=LEGACY_PATH))
    def test_denials_are_caught_as_the_fork_class(self):
        install_legacy_exception_base()
        self.assertTrue(
            issubclass(TransitionNotAllowed, LegacyTransitionNotAllowed))
        try:
            raise TransitionNotAllowed('denied')
        except LegacyTransitionNotAllowed:
            pass

    @override_settings(DJANGO_LOGIC=_conf(LEGACY_EXCEPTION_BASE=LEGACY_PATH))
    def test_bridge_propagates_to_every_denial_subclass(self):
        """The fork's handlers see one exception type; ours has a hierarchy
        under TransitionNotAllowed — the base must reach all of it via MRO."""
        install_legacy_exception_base()
        for denial in (TransitionTemporarilyUnavailable,
                       AlreadyInProgress, SourceStateChanged):
            with self.subTest(denial=denial.__name__):
                with self.assertRaises(LegacyTransitionNotAllowed):
                    raise denial('denied')

    def test_unset_setting_is_a_noop(self):
        before = TransitionNotAllowed.__bases__
        install_legacy_exception_base()
        self.assertEqual(TransitionNotAllowed.__bases__, before)

    @override_settings(DJANGO_LOGIC=_conf(LEGACY_EXCEPTION_BASE=LEGACY_PATH))
    def test_double_install_adds_the_base_once(self):
        # Both apps' ready() call the installer, and Django re-enters
        # ready() freely — the base must not stack.
        install_legacy_exception_base()
        install_legacy_exception_base()
        self.assertEqual(
            TransitionNotAllowed.__bases__.count(LegacyTransitionNotAllowed),
            1)


class BridgeRejectionTests(_BasesCleanup, SimpleTestCase):
    def assert_install_rejected(self, path):
        with override_settings(DJANGO_LOGIC=_conf(LEGACY_EXCEPTION_BASE=path)):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                install_legacy_exception_base()
        self.assertIn('LEGACY_EXCEPTION_BASE', str(ctx.exception))
        # A refused bridge must leave the hierarchy untouched.
        self.assertEqual(TransitionNotAllowed.__bases__, self._saved_bases)

    def test_unimportable_path_rejected(self):
        for path in ('tests.no_such_module.Nope',
                     'tests.test_legacy_exception_base.NoSuchName'):
            with self.subTest(path=path):
                self.assert_install_rejected(path)

    def test_non_class_rejected(self):
        self.assert_install_rejected(
            'tests.test_legacy_exception_base.NOT_A_CLASS')

    def test_non_exception_class_rejected(self):
        self.assert_install_rejected(
            'tests.test_legacy_exception_base.NotAnException')

    def test_mro_conflict_rejected(self):
        self.assert_install_rejected(
            'tests.test_legacy_exception_base.CycleBase')

    def test_incompatible_constructor_rejected_and_rolled_back(self):
        self.assert_install_rejected(
            'tests.test_legacy_exception_base.ArgfulLegacyBase')
        # The probe unwound the mutation, so denials still construct.
        TransitionNotAllowed('still constructible')

    def test_message_eating_base_rejected_and_rolled_back(self):
        self.assert_install_rejected(
            'tests.test_legacy_exception_base.MessageEatingLegacyBase')
        # Denial messages still work after the unwind.
        self.assertEqual(str(TransitionNotAllowed('kept')), 'kept')

    def test_base_exception_during_probe_still_unwinds(self):
        with override_settings(DJANGO_LOGIC=_conf(
            LEGACY_EXCEPTION_BASE=(
                'tests.test_legacy_exception_base.SystemExitingLegacyBase'),
        )):
            # Not translated to ImproperlyConfigured — SystemExit must
            # propagate as itself — but the class must not stay
            # half-mutated.
            with self.assertRaises(SystemExit):
                install_legacy_exception_base()
        self.assertEqual(TransitionNotAllowed.__bases__, self._saved_bases)
        probe = TransitionNotAllowed('kept')
        self.assertEqual(probe.args, ('kept',))

    def test_non_str_values_rejected_at_boot(self):
        for bad in (123, True, [LEGACY_PATH], b'legacy', ''):
            with self.subTest(value=bad):
                with override_settings(
                    DJANGO_LOGIC=_conf(LEGACY_EXCEPTION_BASE=bad)
                ):
                    with self.assertRaises(ImproperlyConfigured) as ctx:
                        validate_core_settings()
                self.assertIn('LEGACY_EXCEPTION_BASE', str(ctx.exception))


class LegacyBridgeProcess(Process):
    process_name = 'legacy_bridge_proc'
    transitions = [
        Transition('approve', sources=['draft'], target='approved'),
    ]


class ForkGuardEndToEndTests(_BasesCleanup, TestCase):
    """The issue's own guard: a shared handler written for the fork's
    exception must catch this engine's denial on a bound model."""

    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, LegacyBridgeProcess, state_field='status')
        cache.clear()

    def tearDown(self):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class is not LegacyBridgeProcess
        ]
        if LegacyBridgeProcess.process_name in vars(Invoice):
            delattr(Invoice, LegacyBridgeProcess.process_name)
        super().tearDown()

    @override_settings(DJANGO_LOGIC=_conf(LEGACY_EXCEPTION_BASE=LEGACY_PATH))
    def test_invalid_transition_is_caught_by_the_forks_handler(self):
        install_legacy_exception_base()
        invoice = Invoice.objects.create(status='paid')
        try:
            invoice.legacy_bridge_proc.approve()
        except LegacyTransitionNotAllowed:
            pass
        else:
            self.fail('the denial escaped the fork-typed handler')
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')


class UnknownSettingsSilenceTests(SimpleTestCase):
    @override_settings(DJANGO_LOGIC={'LEGACY_EXCEPTION_BASE': LEGACY_PATH})
    def test_the_key_is_known_to_w004(self):
        self.assertEqual(check_no_unknown_settings(None), [])
