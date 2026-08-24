"""Assertions in ``django_logic.testing`` that used to pass while checking
nothing.

Each test here fails if its guard is removed:

* a bare string where a list of names belongs must raise, not iterate the
  string one character at a time.
* ``from_snapshot`` must reject a snapshot of another model, and must own the
  restored instance's ``TransitionMessage`` row.
* the wrong entrypoint, and an injection that never fired, must fail at once
  rather than later for some other reason.
* ``process_name`` derives from ``process_class`` when a scenario declares no
  name of its own.
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


# --- A bare string where a list of names belongs ----------------------------


class BareStringNameArgumentTests(ProcessScenario):
    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

    def test_assert_not_available_rejects_a_bare_string(self):
        # 'approve' is available, but iterating the string one character at a
        # time matched nothing, so the assertion used to pass.
        widget = self.create_instance(status='draft')
        with self.assertRaises(TypeError) as ctx:
            self.assert_not_available(widget, 'approve')
        self.assertIn('must be a list of names', str(ctx.exception))
        self.assertIn("['approve']", str(ctx.exception))

    def test_assert_side_effects_not_ran_rejects_a_bare_string(self):
        # 'se_a' did run, but per-character iteration made the assertion pass.
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


# --- from_snapshot ----------------------------------------------------------


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
        # It wrote nothing, so there is no half-restored instance.
        self.assertFalse(Invoice.objects.exists())

    def test_scenario_from_snapshot_rejects_a_snapshot_of_another_model(self):
        widget = self.create_instance(status='draft')
        data = self.snapshot(widget)
        data['model'] = Invoice._meta.label
        with self.assertRaises(ValueError):
            self.from_snapshot(data)

    def test_restore_deletes_the_existing_transition_message(self):
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'fulfil', fail_side_effect='bg_ok',
            fail_with=ValueError('snap'))
        data = self.snapshot(widget)

        # The database still holds a row for this instance and process, left
        # over from an earlier run or driven since the snapshot was taken.
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

    def test_snapshot_round_trips_the_retry_clock(self):
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'fulfil', fail_side_effect='bg_ok',
            fail_with=ValueError('hung'))

        # A row from production that started an attempt, timed out, and whose
        # cleanup hook also raised. A timeout-incident test replays this shape.
        now = timezone.now().replace(microsecond=123456)
        transition_message = TransitionMessage.objects.get(
            instance_id=str(widget.pk), process_name='process')
        transition_message.started_at = now - datetime.timedelta(minutes=30)
        transition_message.completed_at = now - datetime.timedelta(minutes=20)
        transition_message.duration_ms = 601_000
        transition_message.failure_side_effect_error = 'cleanup exploded'
        transition_message.last_error_dt = now - datetime.timedelta(minutes=25)
        transition_message.save(
            update_fields=['started_at', 'completed_at', 'duration_ms',
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
        self.assertEqual(replayed.started_at, transition_message.started_at)
        self.assertEqual(replayed.completed_at, transition_message.completed_at)
        self.assertEqual(replayed.last_error_dt,
                         transition_message.last_error_dt)
        self.assertEqual(replayed.duration_ms, 601_000)
        self.assertEqual(replayed.failure_side_effect_error, 'cleanup exploded')


# --- The wrong entrypoint for a background transition ------------------------


class BackgroundEntrypointGuardTests(ProcessScenario):
    process_class = WidgetProcess
    model = Widget
    state_field = 'status'
    process_name = 'process'

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='celery'))
    def test_transition_refuses_a_background_action_under_celery_mode(self):
        # A test environment left on the global default. This used to enqueue a
        # task no worker runs, so the test failed later and somewhere else, with
        # an uncompleted row left behind.
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
    """One action_name declared both synchronously and in the background.
    ``transition()`` drives the synchronous one, so the guard must allow it."""

    process_class = MixedSyncBgProcess
    model = Conversation
    state_field = 'status'
    process_name = 'mixed_process'

    def test_sync_namesake_is_still_driven_by_transition(self):
        conversation = self.create_instance(status='open',
                                            source_integration='gmail')
        self.transition(conversation, 'archive')
        self.assert_state(conversation, 'archived_sync')


# --- An injection that never fired must not pass on another hook's error ----


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
