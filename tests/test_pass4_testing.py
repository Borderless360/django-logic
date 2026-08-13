"""Pass-4 review pins for ``django_logic.testing``.

Every test here fails if the fix it names is reverted — the findings were all
of the "the guard/report says PASS while checking nothing" kind, so each needs
a test that only the fixed code can satisfy:

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

from django.test import override_settings
from django.utils import timezone

from django_logic.background.models import TransitionMessage
from django_logic.testing import ProcessScenario
from django_logic.testing.snapshot import from_snapshot, snapshot
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
