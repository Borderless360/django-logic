"""Regressions for GitHub issues #94, #95, #96 (django_logic.testing).

#94 — a requested ``fail_side_effect`` that never fires must fail the test
loudly instead of silently running the happy path.

#95 — ``snapshot``/``from_snapshot`` must round-trip JSONField values as
real dicts/lists (not Python reprs) and return an instance whose in-memory
attributes are DB-coerced field types.

#96 — tracking instruments the whole process tree, so hooks executed via
``next_transition`` follow-ups are visible to the side-effect assertions.
"""
from django.test import override_settings

from django_logic.testing import ProcessScenario
from django_logic.testing.snapshot import _jsonable, from_snapshot, snapshot
# WidgetChainProcess (approve chains into notify via next_transition) and its
# RAN call-order log live in tests.background.models and are bound in
# tests/background/apps.py — the single binding site for the app.
from tests.background.models import (
    RAN, Widget, WidgetChainProcess, WidgetProcess,
)


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class InjectionMustFireTests(ProcessScenario):
    """#94 — silent injection no-ops are now loud failures."""

    process_class = WidgetProcess
    model = Widget
    state_field = 'status'
    process_name = 'process'

    def test_unknown_fail_side_effect_rejected_eagerly(self):
        widget = self.create_instance()
        with self.assertRaises(ValueError) as ctx:
            self.background_transition(
                widget, 'fulfil',
                fail_side_effect='renamed_hook_that_does_not_exist',
                fail_with=RuntimeError('x'))
        self.assertIn('does not match any side-effect', str(ctx.exception))

    def test_existing_hook_that_never_fires_fails_the_drive(self):
        # 'bg_ok' exists (on fulfil and others) but 'cancel' is a sync
        # transition with no side-effects — the injection can never fire.
        # Pre-fix this recorded OK and the test became a happy-path run.
        widget = self.create_instance()
        with self.assertRaises(AssertionError) as ctx:
            self.transition(widget, 'cancel',
                            fail_side_effect='bg_ok',
                            fail_with=RuntimeError('x'))
        self.assertIn('never fired', str(ctx.exception))

    def test_injection_that_fires_still_works(self):
        widget = self.create_instance()
        self.background_transition(widget, 'fulfil',
                                   fail_side_effect='bg_ok',
                                   fail_with=RuntimeError('boom'))
        self.assert_state(widget, 'fulfilling')
        self.assert_error_recorded(widget, 'boom')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class TrackingCoversNextTransitionTests(ProcessScenario):
    """#96 — hooks run via next_transition are tracked."""

    process_class = WidgetChainProcess
    model = Widget
    state_field = 'status'
    process_name = 'chain_process'

    def setUp(self):
        super().setUp()
        RAN.clear()

    def test_followup_side_effect_is_visible_to_assertions(self):
        widget = self.create_instance()
        self.transition(widget, 'approve')
        self.assert_state(widget, 'notified')          # the chain ran
        self.assertEqual(RAN, ['chain_first', 'chain_followup'])
        # Pre-fix, only 'approve' was instrumented: chain_followup ran but
        # was invisible — assert_side_effects_ran could not see it and
        # assert_side_effects_not_ran(['chain_followup']) passed vacuously.
        self.assert_side_effects_ran(['chain_first', 'chain_followup'])


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class SnapshotFidelityTests(ProcessScenario):
    """#95 — JSONField round-trip + DB-coerced attribute types."""

    process_class = WidgetProcess
    model = Widget
    state_field = 'status'
    process_name = 'process'

    def test_jsonfield_round_trips_as_a_real_list(self):
        widget = self.create_instance()
        widget.kwargs_seen = ['user_id', 'when', {'nested': [1, 2]}]
        widget.save(update_fields=['kwargs_seen'])
        widget.refresh_from_db()

        data = snapshot(widget, state_field='status')
        # Captured as a JSON tree, not a Python repr string.
        self.assertEqual(
            data['fields']['kwargs_seen'],
            ['user_id', 'when', {'nested': [1, 2]}],
        )

        widget.delete()
        restored = from_snapshot(data, model=Widget)
        self.assertIsInstance(restored.kwargs_seen, list)
        self.assertEqual(
            restored.kwargs_seen,
            ['user_id', 'when', {'nested': [1, 2]}],
        )

    def test_restored_instance_attributes_are_db_coerced(self):
        widget = self.create_instance()
        data = snapshot(widget, state_field='status')
        widget.delete()
        restored = from_snapshot(data, model=Widget)
        # refresh_from_db() ran: JSONField attrs are real lists, pk is the
        # model's real pk type, not whatever the JSON file carried.
        self.assertIsInstance(restored.kwargs_seen, list)
        self.assertEqual(restored.pk, data['pk'])

    def test_unsupported_field_value_fails_loudly(self):
        with self.assertRaises(TypeError) as ctx:
            _jsonable(object())
        self.assertIn('unsupported field value type', str(ctx.exception))

    def test_snapshot_round_trips_transition_message_field_name(self):
        # A restored row must take the same phase-2 path as the production
        # row — field_name='' would route it down the legacy inference
        # fallback instead of the recorded-field path.
        from django_logic.background import sync_execution
        from django_logic.background.models import TransitionMessage

        widget = self.create_instance()
        with sync_execution():
            widget.process.fulfil()
        data = snapshot(widget, state_field='status')
        self.assertEqual(data['transition_message']['field_name'], 'status')

        TransitionMessage.objects.all().delete()
        widget.delete()
        restored = from_snapshot(data, model=Widget)
        tm = TransitionMessage.objects.get(instance_id=str(restored.pk))
        self.assertEqual(tm.field_name, 'status')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class FailureHookAssertionsTests(ProcessScenario):
    """The failure-hook tracker sinks are assertable (review follow-up)."""

    process_class = WidgetProcess
    model = Widget
    state_field = 'status'
    process_name = 'process'

    @override_settings(DJANGO_LOGIC=dict(_SYNC_SETTINGS,
                                         TRANSITION_MESSAGE_MAX_ERRORS=1))
    def test_failure_hooks_are_assertable(self):
        # crash_with_bad_cleanup: side-effect raises; terminal at
        # MAX_ERRORS=1; its failure_side_effect (bg_fse_boom) raises too —
        # so only failure_callbacks complete. Use 'crash' which declares
        # failure_callbacks=[bg_failure_callback].
        widget = self.create_instance()
        self.background_transition(widget, 'crash',
                                   fail_side_effect='bg_boom',
                                   fail_with=ValueError('kaput'))
        self.assert_state(widget, 'crash_failed')
        self.assert_failure_callbacks_ran(['bg_failure_callback'])


class BeatScheduleTests(ProcessScenario):
    """beat_schedule() consumes STARTER_QUEUE and names the real tasks."""

    process_class = WidgetProcess
    model = Widget

    def test_routes_all_five_tasks_to_the_starter_queue(self):
        from django_logic.background import beat_schedule

        with override_settings(DJANGO_LOGIC={'STARTER_QUEUE': 'my.starter'}):
            schedule = beat_schedule(retry_seconds=30.0)
        self.assertEqual(len(schedule), 5)
        self.assertEqual(
            {entry['options']['queue'] for entry in schedule.values()},
            {'my.starter'},
        )
        self.assertEqual(
            schedule['django-logic-retry-stale'],
            {'task': 'django_logic.retry_stale_transitions',
             'schedule': 30.0, 'options': {'queue': 'my.starter'}},
        )
        # Every entry names a task that actually exists in the registry.
        from django_logic.background import tasks
        registered = {
            tasks.run_background_transition_task.name,
            tasks.retry_stale_transitions.name,
            tasks.cleanup_completed_transitions.name,
            tasks.detect_stuck_transitions.name,
            tasks.watchdog_stale_attempts.name,
            tasks.recover_stranded_states.name,
        }
        for entry in schedule.values():
            self.assertIn(entry['task'], registered)
