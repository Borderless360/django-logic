"""Regressions in ``django_logic.testing``.

* A requested ``fail_side_effect`` that never fires must fail the test loudly
  instead of quietly running the happy path.
* ``snapshot`` and ``from_snapshot`` must round-trip JSONField values as real
  dicts and lists, and return an instance whose attributes carry the database
  field types.
* Tracking instruments the whole process tree, so hooks that run through a
  ``next_transition`` follow-up are visible to the side-effect assertions.
"""
from django.test import override_settings

from django_logic.testing import ProcessScenario
from django_logic.testing.snapshot import _jsonable, from_snapshot, snapshot
# WidgetChainProcess chains approve into notify, and its RAN call log lives in
# tests.background.models. tests/background/apps.py is the one place that binds
# it.
from tests.background.models import (
    RAN, Widget, WidgetChainProcess, WidgetProcess,
)
from tests import dl_settings


class InjectionMustFireTests(ProcessScenario):
    """An injection that never fires is a loud failure, not a silent no-op."""

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
        # 'bg_ok' exists on other transitions, but 'cancel' is synchronous and
        # has no side-effects, so the injection can never fire. It used to
        # record a pass and quietly run the happy path.
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


class TrackingCoversNextTransitionTests(ProcessScenario):
    """Hooks that run through next_transition are tracked."""

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
        # Only 'approve' used to be instrumented, so chain_followup ran unseen
        # and assert_side_effects_not_ran(['chain_followup']) passed for the
        # wrong reason.
        self.assert_side_effects_ran(['chain_first', 'chain_followup'])


class SnapshotFidelityTests(ProcessScenario):
    """snapshot round-trips JSONField values and database field types."""

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
        # refresh_from_db() ran, so JSONField attributes are real lists and pk
        # has the model's field type, not the type the JSON file carried.
        self.assertIsInstance(restored.kwargs_seen, list)
        self.assertEqual(restored.pk, data['pk'])

    def test_unsupported_field_value_fails_loudly(self):
        with self.assertRaises(TypeError) as ctx:
            _jsonable(object())
        self.assertIn('unsupported field value type', str(ctx.exception))

    def test_snapshot_round_trips_transition_message_field_name(self):
        # A restored row must take the same worker path as a production row.
        # With field_name='' the worker infers the state field instead of
        # reading the recorded one.
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
        row = TransitionMessage.objects.get(instance_id=str(restored.pk))
        self.assertEqual(row.field_name, 'status')


class FailureHookAssertionsTests(ProcessScenario):
    """The failure hooks the tracker records are assertable."""

    process_class = WidgetProcess
    model = Widget
    state_field = 'status'
    process_name = 'process'

    @override_settings(DJANGO_LOGIC=dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=1))
    def test_failure_hooks_are_assertable(self):
        # 'crash' declares failure_callbacks=[bg_failure_callback], and
        # MAX_ERRORS=1 makes the first attempt terminal, so the callback runs.
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

    def test_routes_all_four_tasks_to_the_starter_queue(self):
        from django_logic.background import beat_schedule

        with override_settings(DJANGO_LOGIC={'STARTER_QUEUE': 'my.starter'}):
            schedule = beat_schedule(retry_seconds=30.0)
        self.assertEqual(len(schedule), 4)
        self.assertEqual(
            {entry['options']['queue'] for entry in schedule.values()},
            {'my.starter'},
        )
        self.assertEqual(
            schedule['django-logic-retry-stale'],
            {'task': 'django_logic.retry_stale_transitions',
             'schedule': 30.0, 'options': {'queue': 'my.starter'}},
        )
        # Cleanup must be a wall-clock schedule, not an interval: interval
        # entries count from beat start-up, and a beat that restarts on
        # every deploy (or daily, on platforms that cycle dynos) never
        # reaches a day-scale interval, so cleanup silently never ran.
        from celery.schedules import crontab
        self.assertIsInstance(
            schedule['django-logic-cleanup']['schedule'], crontab)
        # Every entry names a task that actually exists in the registry.
        from django_logic.background import tasks
        registered = {
            tasks.run_background_transition_task.name,
            tasks.retry_stale_transitions.name,
            tasks.cleanup_completed_transitions.name,
            tasks.detect_stuck_transitions.name,
            tasks.watchdog_stale_attempts.name,
        }
        for entry in schedule.values():
            self.assertIn(entry['task'], registered)
