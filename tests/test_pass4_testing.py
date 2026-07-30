"""Pass-4 review pins for ``django_logic.testing`` + the coverage key.

Every test here fails if the fix it names is reverted — the findings were all
of the "the guard/report says PASS while checking nothing" kind, so each needs
a test that only the fixed code can satisfy:

* condition fingerprints must separate per-variant callable-instance
  conditions (``CourierIs('ups')`` vs ``CourierIs('dhl')``) and must be
  byte-identical across processes (the spawn-parallel / separate-report-process
  flow merges keys written by other interpreters).
* a bare string where a list of names belongs is a vacuous assertion.
* ``from_snapshot`` must reject a foreign snapshot and must own the restored
  instance's ``TransitionMessage`` row.
* the wrong entrypoint (``transition()`` on a background action) and an
  injection that never fired must fail immediately, not later for another
  reason.
* ``process_name`` derives from ``process_class`` — pinned by a scenario that
  does not declare one.
"""
import datetime
import functools
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

import django_logic
from django_logic.background.models import TransitionMessage
from django_logic.coverage import (
    TransitionCoverage,
    _condition_fingerprint,
    coverage_report,
)
from django_logic.process import Process, ProcessManager
from django_logic.testing import ProcessScenario
from django_logic.testing.snapshot import from_snapshot, snapshot
from django_logic.transition import Transition
from tests import dl_settings
from tests.background.models import (
    Conversation,
    MixedSyncBgProcess,
    Widget,
    WidgetAmbiguousNextProcess,
    WidgetBgChainProcess,
    WidgetProcess,
    WidgetSyncProcess,
)
from tests.models import Invoice


# --- B-K4 + B1: condition fingerprint identity & cross-process stability ---


class _CourierIs:
    """The idiomatic per-variant condition: one class, one instance per
    courier. Its identity as a *declaration* is the courier it carries."""

    def __init__(self, courier):
        self.courier = courier

    def __call__(self, instance, **kwargs):
        return getattr(instance, '_courier', None) == self.courier


class _SlottedCourierIs:
    __slots__ = ('courier',)

    def __init__(self, courier):
        self.courier = courier

    def __call__(self, instance, **kwargs):
        return getattr(instance, '_courier', None) == self.courier


def _is_courier(instance, courier=None, client=None, **kwargs):
    return getattr(instance, '_courier', None) == courier


class _Opaque:
    """No stable ``repr``: the default one embeds a process-local address."""


# Run in a FRESH interpreter (twice) — the addresses in a default repr differ
# per process, which is exactly what breaks a merged coverage log.
_FINGERPRINT_SNIPPET = '''
import functools
from django_logic.coverage import _condition_fingerprint


class Opaque:
    pass


class CourierIs:
    def __init__(self, courier, client):
        self.courier = courier
        self.client = client

    def __call__(self, instance, **kwargs):
        return True


def is_courier(instance, courier=None, client=None, **kwargs):
    return True


print(_condition_fingerprint(CourierIs('ups', Opaque())))
print(_condition_fingerprint(
    functools.partial(is_courier, 'ups', client=Opaque())))
'''


class ConditionFingerprintTests(SimpleTestCase):
    def test_same_class_different_config_are_different_keys(self):
        ups = _condition_fingerprint(_CourierIs('ups'))
        dhl = _condition_fingerprint(_CourierIs('dhl'))
        self.assertNotEqual(ups, dhl)
        self.assertIn("courier='ups'", ups)
        self.assertIn("courier='dhl'", dhl)

    def test_slotted_instances_without_a_dict_still_differ(self):
        ups = _condition_fingerprint(_SlottedCourierIs('ups'))
        dhl = _condition_fingerprint(_SlottedCourierIs('dhl'))
        self.assertNotEqual(ups, dhl)
        self.assertIn('_SlottedCourierIs', ups)

    def test_instance_config_without_a_stable_repr_degrades_to_a_class_path(self):
        fingerprint = _condition_fingerprint(_CourierIs(_Opaque()))
        self.assertNotIn('0x', fingerprint)
        self.assertIn('_Opaque', fingerprint)

    def test_partial_keeps_primitive_config_readable(self):
        fingerprint = _condition_fingerprint(
            functools.partial(_is_courier, 'ups', client=None))
        self.assertIn('_is_courier', fingerprint)
        self.assertIn("'ups'", fingerprint)
        self.assertIn('client=None', fingerprint)
        self.assertNotIn('0x', fingerprint)

    def test_partial_config_types_still_separate_declarations(self):
        self.assertNotEqual(
            _condition_fingerprint(functools.partial(_is_courier, courier='ups')),
            _condition_fingerprint(functools.partial(_is_courier, courier='dhl')),
        )

    def test_fingerprints_are_identical_in_two_separate_processes(self):
        first = self._fingerprints_from_a_fresh_process()
        second = self._fingerprints_from_a_fresh_process()
        self.assertEqual(first, second)
        # The reason they used to differ: an address baked into the key.
        self.assertNotIn('0x', first)

    def _fingerprints_from_a_fresh_process(self) -> str:
        repo_root = Path(django_logic.__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, '-c', _FINGERPRINT_SNIPPET],
            cwd=str(repo_root), capture_output=True, text=True, timeout=120,
            env={'PYTHONPATH': str(repo_root), 'PATH': '/usr/bin:/bin'},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout


class CallableConditionCoverageIdentityTests(TestCase):
    """Two per-courier variants of one action must count — and cover —
    separately. Sharing a key made driving ``ups`` mark ``dhl`` covered, i.e.
    the coverage gate greenlit a transition no test ever drove."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        class Pass4CourierProcess(Process):
            process_name = 'pass4_courier_process'
            transitions = [
                Transition('ship', sources=['ready'], target='shipped',
                           conditions=[_CourierIs('ups')]),
                Transition('ship', sources=['ready'], target='shipped',
                           conditions=[_CourierIs('dhl')]),
            ]

        cls.process_class = Pass4CourierProcess
        ProcessManager.bind_model_process(Invoice, Pass4CourierProcess,
                                          state_field='status')

    @classmethod
    def tearDownClass(cls):
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings
            if b.process_class is not cls.process_class
        ]
        if 'pass4_courier_process' in vars(Invoice):
            delattr(Invoice, 'pass4_courier_process')
        super().tearDownClass()

    def _ours(self, report):
        return [u for u in report['uncovered']
                if u['process'].endswith('Pass4CourierProcess')]

    def test_each_configured_variant_counts_separately(self):
        self.assertEqual(len(self._ours(coverage_report(executed=()))), 2)

    def test_driving_one_courier_leaves_the_other_uncovered(self):
        invoice = Invoice.objects.create(status='ready')
        invoice._courier = 'ups'
        with TransitionCoverage() as cov:
            invoice.pass4_courier_process.ship()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'shipped')
        self.assertEqual(len(self._ours(cov.report())), 1)


# --- B2: a bare string where a list of names belongs ------------------------


class BareStringNameArgumentTests(ProcessScenario):
    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

    def test_assert_not_available_rejects_a_bare_string(self):
        # 'approve' IS available, yet iterating it per character found no
        # match — the assertion passed while asserting nothing.
        widget = self.create_instance(status='draft')
        with self.assertRaises(TypeError) as ctx:
            self.assert_not_available(widget, 'approve')
        self.assertIn('must be a list of names', str(ctx.exception))
        self.assertIn("['approve']", str(ctx.exception))

    def test_assert_side_effects_not_ran_rejects_a_bare_string(self):
        # 'se_a' DID run; per-character iteration made the assertion pass.
        widget = self.create_instance(status='draft')
        self.transition(widget, 'approve')
        with self.assertRaises(TypeError) as ctx:
            self.assert_side_effects_not_ran('se_a')
        self.assertIn('must be a list of names', str(ctx.exception))

    def test_every_name_collection_argument_rejects_a_bare_string(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'approve')
        calls = {
            'assert_available': lambda: self.assert_available(widget, 'approve'),
            'assert_not_available':
                lambda: self.assert_not_available(widget, 'approve'),
            'assert_side_effects_ran':
                lambda: self.assert_side_effects_ran('se_a'),
            'assert_side_effects_not_ran':
                lambda: self.assert_side_effects_not_ran('se_a'),
            'assert_callbacks_ran':
                lambda: self.assert_callbacks_ran('cb_after_approve'),
            'assert_failure_side_effects_ran':
                lambda: self.assert_failure_side_effects_ran('fse_cleanup'),
            'assert_failure_callbacks_ran':
                lambda: self.assert_failure_callbacks_ran('fcb_on_fail'),
            'capture': lambda: self.capture(widget, 'status'),
            'assert_unchanged':
                lambda: self.assert_unchanged(widget, {}, 'status'),
        }
        for name, call in calls.items():
            with self.subTest(assertion=name):
                with self.assertRaises(TypeError):
                    call()


# --- B3 + B4 + B-K2: from_snapshot -----------------------------------------


class SnapshotRestoreGuardTests(ProcessScenario):
    process_class = WidgetProcess
    model = Widget
    state_field = 'status'
    process_name = 'process'

    def test_wrong_model_snapshot_is_rejected_loudly(self):
        widget = self.create_instance(status='draft')
        data = snapshot(widget, state_field='status')
        with self.assertRaises(ValueError) as ctx:
            from_snapshot(data, model=Invoice)
        message = str(ctx.exception)
        self.assertIn('bg_tests.Widget', message)
        self.assertIn(Invoice._meta.label, message)
        # Nothing was written: the corrupted half-restore never happened.
        self.assertFalse(Invoice.objects.exists())

    def test_scenario_from_snapshot_rejects_a_foreign_snapshot(self):
        widget = self.create_instance(status='draft')
        data = self.snapshot(widget)
        data['model'] = Invoice._meta.label      # the wrong snapshot file
        with self.assertRaises(ValueError):
            self.from_snapshot(data)

    def test_restore_purges_the_existing_transition_message(self):
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'fulfil', fail_side_effect='bg_ok',
            fail_with=ValueError('snap'))
        data = self.snapshot(widget)

        # The repro DB still holds a row for this instance+process (a stale
        # orphan, or a row driven since the snapshot was taken).
        stale = TransitionMessage.objects.get(instance_id=str(widget.pk),
                                              process_name='process')
        stale.errors_count = 99
        stale.last_error_message = 'stale orphan'
        stale.save(update_fields=['errors_count', 'last_error_message'])
        Widget.objects.filter(pk=widget.pk).delete()

        restored = self.from_snapshot(data)
        rows = TransitionMessage.objects.filter(
            instance_id=str(restored.pk), process_name='process')
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().errors_count, 1)
        self.assertNotIn('stale orphan', rows.get().last_error_message)

    def test_snapshot_round_trips_the_retry_and_watchdog_clock(self):
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'fulfil', fail_side_effect='bg_ok',
            fail_with=ValueError('hung'))

        # A production row that started an attempt, timed out, and had its
        # cleanup hook blow up as well — the shape a watchdog test replays.
        now = timezone.now().replace(microsecond=123456)
        tm = TransitionMessage.objects.get(instance_id=str(widget.pk),
                                          process_name='process')
        tm.started_at = now - datetime.timedelta(minutes=30)
        tm.completed_at = now - datetime.timedelta(minutes=20)
        tm.duration_ms = 601_000
        tm.failure_side_effect_error = 'cleanup exploded'
        tm.last_error_dt = now - datetime.timedelta(minutes=25)
        tm.save(update_fields=['started_at', 'completed_at', 'duration_ms',
                               'failure_side_effect_error', 'last_error_dt'])

        data = self.snapshot(widget)
        for key in ('started_at', 'completed_at', 'duration_ms',
                    'failure_side_effect_error', 'last_error_dt'):
            self.assertIn(key, data['transition_message'])

        TransitionMessage.objects.all().delete()
        Widget.objects.filter(pk=widget.pk).delete()

        restored = self.from_snapshot(data)
        replayed = TransitionMessage.objects.get(
            instance_id=str(restored.pk), process_name='process')
        self.assertEqual(replayed.started_at, tm.started_at)
        self.assertEqual(replayed.completed_at, tm.completed_at)
        self.assertEqual(replayed.last_error_dt, tm.last_error_dt)
        self.assertEqual(replayed.duration_ms, 601_000)
        self.assertEqual(replayed.failure_side_effect_error, 'cleanup exploded')


# --- B5: the wrong entrypoint for a background transition ------------------


class BackgroundEntrypointGuardTests(ProcessScenario):
    process_class = WidgetProcess
    model = Widget
    state_field = 'status'
    process_name = 'process'

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='celery'))
    def test_transition_refuses_a_background_action_under_celery_mode(self):
        # A consumer test env on the global default: pre-fix this enqueued a
        # task no worker runs and the test failed later, elsewhere, for the
        # wrong reason — with an uncompleted TransitionMessage left behind.
        widget = self.create_instance(status='draft')
        with self.assertRaises(AssertionError) as ctx:
            self.transition(widget, 'fulfil')
        message = str(ctx.exception)
        self.assertIn('use background_transition', message)
        self.assertNotIn('AlreadyInProgress', message)
        self.assertEqual(
            TransitionMessage.objects.filter(
                instance_id=str(widget.pk)).count(), 0)
        self.assert_state(widget, 'draft')

    def test_transition_refuses_a_background_action_under_sync_mode_too(self):
        widget = self.create_instance(status='draft')
        with self.assertRaises(AssertionError) as ctx:
            self.transition(widget, 'fulfil')
        self.assertIn('use background_transition', str(ctx.exception))

    def test_background_transition_is_still_the_way_to_drive_it(self):
        widget = self.create_instance(status='draft')
        self.background_transition(widget, 'fulfil')
        self.assert_state(widget, 'fulfilled')


class MixedNamesakeEntrypointTests(ProcessScenario):
    """A sync + background namesake pair: ``transition()`` legitimately drives
    the SYNC declaration, so the guard must not refuse it."""

    process_class = MixedSyncBgProcess
    model = Conversation
    state_field = 'status'
    process_name = 'mixed_process'

    def test_sync_namesake_is_still_driven_by_transition(self):
        conversation = self.create_instance(status='open',
                                            source_integration='gmail')
        self.transition(conversation, 'archive')
        self.assert_state(conversation, 'archived_sync')


# --- B6: an injection that never fired must not borrow another hook's alibi -


class InjectionNeverFiredTests(ProcessScenario):
    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

    def test_matching_exception_from_another_hook_is_not_evidence(self):
        # 'se_c' lives on 'notify' and cannot run during a 'capture_fail'
        # drive; 'capture_fail' raises ValueError from sync_boom, which
        # satisfies expect_raises. Pre-fix the #94 guard stood down because
        # *something* raised, so the injection silently never fired.
        widget = self.create_instance(status='draft')
        with self.assertRaises(AssertionError) as ctx:
            self.transition(widget, 'capture_fail',
                            fail_side_effect='se_c',
                            fail_with=ValueError('injected, never runs'),
                            expect_raises=ValueError)
        message = str(ctx.exception)
        self.assertIn('never fired', message)
        self.assertIn('another hook', message)

    def test_an_injection_that_does_fire_still_passes(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'capture_fail',
                        fail_side_effect='sync_boom',
                        fail_with=ValueError('injected'),
                        expect_raises=ValueError)
        self.assert_state(widget, 'capture_failed')


# --- B7/E7: process_name derives from process_class ------------------------


class DerivedProcessNameSyncTests(ProcessScenario):
    """No ``process_name`` here on purpose: it must derive from
    ``process_class.process_name`` ('ambig_next'). Every other scenario in the
    suite declares one, so the derivation was unpinned."""

    process_class = WidgetAmbiguousNextProcess
    model = Widget
    state_field = 'status'

    def test_derived_accessor_drives_the_right_machine(self):
        self.assertEqual(self._process_name, 'ambig_next')
        widget = self.create_instance(status='draft')
        self.transition(widget, 'start')
        self.assert_state(widget, 'started')
        self.assert_side_effects_ran(['se_start'])


class DerivedProcessNameBackgroundTests(ProcessScenario):
    """The derived accessor must also scope the TransitionMessage lookups —
    ``process_name=None`` matched no row and made them pass/fail for the
    wrong reason."""

    process_class = WidgetBgChainProcess
    model = Widget
    state_field = 'status'

    def test_message_lookups_use_the_derived_accessor(self):
        self.assertEqual(self._process_name, 'bg_chain')
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'bg_fulfil', fail_side_effect='se_bg_fulfil_se',
            fail_with=ValueError('courier down'))
        self.assert_state(widget, 'chain_fulfilling')
        self.assert_error_recorded(widget, 'courier down')
        self.assert_error_count(widget, 1)
        self.retry_transition(widget)          # uncompleted_message lookup
        self.assert_state(widget, 'exported')
