"""Transition-initiation observers + transition-execution coverage (#132).

Static test-tree analysis cannot see transitive or dynamically-dispatched
drives; the resolver can. These tests pin the observer contract (fires on
every initiation path, never breaks a transition) and the coverage report
(executed vs declared, nested owner attribution, file-backed merging).
"""
import tempfile

from django.test import TestCase

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
        observer = lambda cls, action, instance: seen.append((cls, action, instance.pk))  # noqa: E731
        transition_observers.append(observer)
        try:
            invoice.coverage_process.approve()
        finally:
            transition_observers.remove(observer)
        self.assertEqual(seen, [(_CoverageProcess, 'approve', invoice.pk)])

    def test_observer_attributes_nested_transition_to_declaring_process(self):
        invoice = Invoice.objects.create(status='approved')
        seen = []
        observer = lambda cls, action, instance: seen.append((cls, action))  # noqa: E731
        transition_observers.append(observer)
        try:
            invoice.coverage_process.archive()
        finally:
            transition_observers.remove(observer)
        self.assertEqual(seen, [(_NestedCoverageProcess, 'archive')])

    def test_raising_observer_does_not_break_the_transition(self):
        invoice = Invoice.objects.create(status='draft')

        def broken(cls, action, instance):
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
