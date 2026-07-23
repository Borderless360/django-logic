"""Transition-initiation observers + transition-execution coverage (#132).

Static test-tree analysis cannot see transitive or dynamically-dispatched
drives; the resolver can. These tests pin the observer contract (fires on
every initiation path, never breaks a transition) and the coverage report
(executed vs declared, nested owner attribution, file-backed merging).
"""
import tempfile

from django.test import TestCase, override_settings

from django_logic.coverage import (
    TransitionCoverage,
    coverage_report,
    iter_bound_transitions,
    start_file_recording,
    stop_file_recording,
)
from django_logic.process import Process, ProcessManager, transition_observers
from django_logic.transition import Transition
from tests.models import Invoice


def is_never_available(instance):
    return False


class _NestedCoverageProcess(Process):
    process_name = 'coverage_process'
    transitions = [
        Transition('archive', sources=['approved'], target='archived'),
    ]


class _CoverageProcess(Process):
    process_name = 'coverage_process'
    nested_processes = [_NestedCoverageProcess]
    transitions = [
        Transition('approve', sources=['draft'], target='approved'),
        Transition('reject', sources=['draft'], target='rejected'),
        Transition('purge', sources=['archived'], target='purged',
                   conditions=[is_never_available]),
    ]


class TransitionObserverAndCoverageTests(TestCase):
    def setUp(self):
        super().setUp()
        ProcessManager.bind_model_process(
            Invoice, _CoverageProcess, state_field='status')

    def tearDown(self):
        stop_file_recording()
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class is not _CoverageProcess
        ]
        if 'coverage_process' in vars(Invoice):
            delattr(Invoice, 'coverage_process')
        super().tearDown()

    # --- observer contract ------------------------------------------------

    def test_observer_receives_owner_action_and_instance(self):
        invoice = Invoice.objects.create(status='draft')
        seen = []
        observer = lambda cls, action, instance, transition: seen.append(
            (cls, action, instance.pk, transition.action_name))  # noqa: E731
        transition_observers.append(observer)
        try:
            invoice.coverage_process.approve()
        finally:
            transition_observers.remove(observer)
        self.assertEqual(seen, [(_CoverageProcess, 'approve', invoice.pk, 'approve')])

    def test_observer_attributes_nested_transition_to_declaring_process(self):
        invoice = Invoice.objects.create(status='approved')
        seen = []
        observer = lambda cls, action, instance, transition: seen.append((cls, action))  # noqa: E731
        transition_observers.append(observer)
        try:
            invoice.coverage_process.archive()
        finally:
            transition_observers.remove(observer)
        self.assertEqual(seen, [(_NestedCoverageProcess, 'archive')])

    def test_raising_observer_does_not_break_the_transition(self):
        invoice = Invoice.objects.create(status='draft')

        def broken(cls, action, instance, transition):
            raise RuntimeError('observer bug')

        transition_observers.append(broken)
        try:
            with self.assertLogs('django-logic.transition', level='ERROR'):
                invoice.coverage_process.approve()
        finally:
            transition_observers.remove(broken)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'approved')

    # --- declared-transition walk ------------------------------------------

    def test_iter_bound_transitions_includes_nested(self):
        pairs = {
            (proc.__name__, t.action_name)
            for _, proc, t in iter_bound_transitions()
            if proc in (_CoverageProcess, _NestedCoverageProcess)
        }
        self.assertEqual(pairs, {
            ('_CoverageProcess', 'approve'),
            ('_CoverageProcess', 'reject'),
            ('_CoverageProcess', 'purge'),
            ('_NestedCoverageProcess', 'archive'),
        })

    # --- in-memory recorder -------------------------------------------------

    def test_coverage_report_splits_executed_and_uncovered(self):
        invoice = Invoice.objects.create(status='draft')
        with TransitionCoverage() as cov:
            invoice.coverage_process.approve()
            invoice.coverage_process.archive()
        self.assertNotIn(cov._observe, transition_observers)

        report = cov.report()
        ours = [u for u in report['uncovered']
                if u['process'].endswith(('_CoverageProcess', '_NestedCoverageProcess'))]
        self.assertEqual({u['action'] for u in ours}, {'reject', 'purge'})
        self.assertTrue(all(u['models'] == ['tests.Invoice'] for u in ours))
        self.assertTrue(all(u['background'] is False for u in ours))

    # --- file-backed recorder -----------------------------------------------

    def test_file_recording_appends_unique_pairs_and_report_merges(self):
        invoice = Invoice.objects.create(status='draft')
        with tempfile.NamedTemporaryFile(mode='r', suffix='.log') as log:
            start_file_recording(log.name)
            invoice.coverage_process.approve()
            invoice.coverage_process.archive()
            Invoice.objects.filter(pk=invoice.pk).update(status='draft')
            invoice.refresh_from_db()
            invoice.coverage_process.approve()  # dedup: no second line
            stop_file_recording()

            lines = [line for line in log.read().splitlines() if line]
            self.assertEqual(len(lines), 2)

            report = coverage_report(log_path=log.name)
            ours = [u for u in report['uncovered']
                    if u['process'].endswith(('_CoverageProcess', '_NestedCoverageProcess'))]
            self.assertEqual({u['action'] for u in ours}, {'reject', 'purge'})

    def test_start_file_recording_is_idempotent_per_path(self):
        with tempfile.NamedTemporaryFile(suffix='.log') as log:
            start_file_recording(log.name)
            start_file_recording(log.name)
            recorders = [o for o in transition_observers
                         if o.__class__.__name__ == '_FileRecorder']
            self.assertEqual(len(recorders), 1)

    def test_coverage_report_tolerates_a_never_written_log(self):
        # The recorder creates the file on the first pair — a run that drove
        # nothing is a valid all-uncovered report, not a crash.
        report = coverage_report(log_path='/nonexistent/dir/coverage.log')
        self.assertEqual(report['executed'], 0)

    # --- observer isolation / cleanup ---------------------------------------

    def test_dirty_with_block_exit_still_unregisters_the_observer(self):
        cov = TransitionCoverage()
        with self.assertRaises(RuntimeError):
            with cov:
                raise RuntimeError('test body blew up')
        self.assertNotIn(cov._observe, transition_observers)

    def test_raising_observer_does_not_block_later_observers(self):
        invoice = Invoice.objects.create(status='draft')
        seen = []

        def broken(cls, action, instance, transition):
            raise RuntimeError('observer bug')

        def working(cls, action, instance, transition):
            seen.append(action)

        transition_observers.extend([broken, working])
        try:
            with self.assertLogs('django-logic.transition', level='ERROR'):
                invoice.coverage_process.approve()
        finally:
            transition_observers.remove(broken)
            transition_observers.remove(working)
        self.assertEqual(seen, ['approve'])


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}

_BG_FAIL = {'on': False}


def _bg_side_effect(instance, **kwargs):
    if _BG_FAIL['on']:
        raise ValueError('injected failure')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class BackgroundObserverSemanticsTests(TestCase):
    """The headline claims: background phase 1 notifies exactly once;
    phase-2 execution and retries never re-notify; a next_transition
    follow-up notifies once more, attributed to the follow-up's owner."""

    @classmethod
    def setUpClass(cls):
        from django_logic.background import BackgroundTransition
        from tests.background.models import Widget

        super().setUpClass()

        class ObserverBgProcess(Process):
            process_name = 'observer_bg_process'
            transitions = [
                BackgroundTransition('bg_go', sources=['draft'], target='done',
                                     failed_state='failed',
                                     side_effects=[_bg_side_effect]),
                Transition('kick', sources=['draft'], target='kicked',
                           next_transition='bg_finish'),
                BackgroundTransition('bg_finish', sources=['kicked'],
                                     target='done',
                                     side_effects=[_bg_side_effect]),
            ]

        cls.process_class = ObserverBgProcess
        cls.Widget = Widget
        ProcessManager.bind_model_process(Widget, ObserverBgProcess,
                                          state_field='status')

    @classmethod
    def tearDownClass(cls):
        if 'observer_bg_process' in vars(cls.Widget):
            delattr(cls.Widget, 'observer_bg_process')
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class is not cls.process_class
        ]
        super().tearDownClass()

    def setUp(self):
        _BG_FAIL['on'] = False
        self.seen = []
        self._observer = lambda cls_, action, instance, transition: self.seen.append(
            (cls_.__name__, action))
        transition_observers.append(self._observer)
        self.widget = self.Widget.objects.create(status='draft')

    def tearDown(self):
        transition_observers.remove(self._observer)
        super().tearDown()

    def test_background_initiation_notifies_exactly_once(self):
        # Sync execution mode runs phase 1 AND phase 2 inline — phase 2
        # (restore + side-effect execution) must not add a second record.
        self.widget.observer_bg_process.bg_go()
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'done')
        self.assertEqual(self.seen, [('ObserverBgProcess', 'bg_go')])

    def test_retries_do_not_renotify(self):
        from django_logic.background.models import TransitionMessage
        from django_logic.background.runner import run_background_transition

        _BG_FAIL['on'] = True
        with self.assertRaises(ValueError):
            self.widget.observer_bg_process.bg_go()
        tm = TransitionMessage.objects.get(instance_id=str(self.widget.pk),
                                           transition_name='bg_go')
        for _ in range(2):  # drive the remaining attempts as the worker would
            try:
                run_background_transition(tm.pk)
            except ValueError:
                pass
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'failed')
        # Three attempts, one initiation, one record.
        self.assertEqual(self.seen, [('ObserverBgProcess', 'bg_go')])

    def test_next_transition_follow_up_notifies_with_its_own_initiation(self):
        self.widget.observer_bg_process.kick()
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'done')
        self.assertEqual(self.seen, [('ObserverBgProcess', 'kick'),
                                     ('ObserverBgProcess', 'bg_finish')])


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class NamesakeDeclarationIdentityTests(TestCase):
    """#146 — condition-disambiguated same-name transitions (including a
    sync + background namesake pair in one class) count and cover as
    separate declarations; 0.8-era 2-field log lines keep the legacy
    cover-all-namesakes semantics."""

    @classmethod
    def setUpClass(cls):
        from django_logic.background import BackgroundTransition

        super().setUpClass()

        class NamesakeProcess(Process):
            process_name = 'namesake_process'
            transitions = [
                Transition('ship', sources=['fast_lane'], target='shipped_fast'),
                Transition('ship', sources=['slow_lane'], target='shipped_slow'),
                Transition('export', sources=['ready'], target='exported_inline'),
                BackgroundTransition('export', sources=['queued'],
                                     target='exported'),
            ]

        cls.process_class = NamesakeProcess
        ProcessManager.bind_model_process(Invoice, NamesakeProcess,
                                          state_field='status')

    @classmethod
    def tearDownClass(cls):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class is not cls.process_class
        ]
        if 'namesake_process' in vars(Invoice):
            delattr(Invoice, 'namesake_process')
        super().tearDownClass()

    def _ours(self, report):
        return [u for u in report['uncovered']
                if u['process'].endswith('NamesakeProcess')]

    def test_each_namesake_declaration_counts_separately(self):
        report = coverage_report(executed=())
        ours = self._ours(report)
        self.assertEqual(len(ours), 4)
        ships = [u for u in ours if u['action'] == 'ship']
        self.assertEqual({u['target'] for u in ships},
                         {'shipped_fast', 'shipped_slow'})
        exports = [u for u in ours if u['action'] == 'export']
        self.assertEqual({u['background'] for u in exports}, {True, False})

    def test_driving_one_namesake_covers_only_that_declaration(self):
        invoice = Invoice.objects.create(status='fast_lane')
        with TransitionCoverage() as cov:
            invoice.namesake_process.ship()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'shipped_fast')

        ours = self._ours(cov.report())
        self.assertEqual(len(ours), 3)
        remaining_ship = [u for u in ours if u['action'] == 'ship']
        self.assertEqual([u['target'] for u in remaining_ship],
                         ['shipped_slow'])

    def test_legacy_two_field_log_line_covers_all_namesakes(self):
        proc = self.process_class
        legacy_line = f'{proc.__module__}.{proc.__qualname__}\tship'
        report = coverage_report(executed=[legacy_line])
        ours = self._ours(report)
        # Both ship declarations covered (old semantics); both exports not.
        self.assertEqual({u['action'] for u in ours}, {'export'})
        self.assertEqual(len(ours), 2)
