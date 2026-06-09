"""Exercises the scenario-based testing framework (`django_logic.testing`).

Demonstrates the full surface — synchronous transitions, background happy/
failure/retry/terminal paths, BackgroundAction (no state change), availability
with conditions + permissions, side-effect/callback tracking, snapshot capture
+ replay, and AI-readable failure output — all without Celery.

Driven against the existing Widget/WidgetProcess fixtures plus a tiny guarded
process bound under `guard` (no new migrations).
"""
from django.conf import settings
from django.contrib.auth import get_user_model

from django_logic import Process, ProcessManager, Transition
from django_logic.background.models import TransitionMessage
from django_logic.testing import ProcessScenario
from tests.background.models import Widget, WidgetProcess


# --- a minimal process with a condition + permission, bound under `guard` ---

def _stock_ok(instance):
    return getattr(instance, '_stock_available', True)


def _is_staff(instance, user):
    return bool(user and getattr(user, 'is_staff', False))


class ScenarioGuardProcess(Process):
    process_name = 'guard'
    transitions = [
        Transition(
            action_name='approve',
            sources=['draft'],
            target='approved',
            conditions=[_stock_ok],
            permissions=[_is_staff],
        ),
    ]


ProcessManager.bind_model_process(Widget, ScenarioGuardProcess, state_field='status')


class WidgetFulfilmentScenario(ProcessScenario):
    process_class = WidgetProcess
    model = Widget
    state_field = 'status'
    process_name = 'process'

    def test_background_happy_path(self):
        """draft -> fulfilling -> fulfilled, side-effects + callback run."""
        widget = self.create_instance(status='draft')
        self.assert_available(widget, ['fulfil', 'cancel'])

        self.background_transition(widget, 'fulfil')
        self.assert_state(widget, 'fulfilled')
        self.assert_side_effects_ran(['bg_ok', 'bg_record_kwargs'])
        self.assert_callbacks_ran(['bg_callback'])

    def test_failure_records_error_and_stays_in_progress(self):
        """A side-effect fails -> error recorded, instance left in_progress."""
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'fulfil',
            fail_side_effect='bg_ok', fail_with=ValueError('courier down'))

        self.assert_state(widget, 'fulfilling')
        self.assert_error_recorded(widget, 'courier down')
        self.assert_error_count(widget, 1)
        self.assert_side_effects_not_ran(['bg_ok', 'bg_record_kwargs'])

    def test_failure_then_retry_succeeds(self):
        """A transient failure is recovered by the retry path."""
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'fulfil', fail_side_effect='bg_ok', fail_with=ValueError('blip'))
        self.assert_state(widget, 'fulfilling')

        self.retry_transition(widget)  # no injection this time -> succeeds
        self.assert_state(widget, 'fulfilled')
        self.assert_error_count(widget, 1)

    def test_max_retries_exhausted_moves_to_failed_state(self):
        """After MAX_ERRORS failures the instance lands in failed_state."""
        max_errors = settings.DJANGO_LOGIC['TRANSITION_MESSAGE_MAX_ERRORS']
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'fulfil', fail_side_effect='bg_ok', fail_with=ValueError('persistent'))
        for _ in range(max_errors - 1):
            self.retry_transition(
                widget, fail_side_effect='bg_ok', fail_with=ValueError('persistent'))

        self.assert_state(widget, 'fulfilment_failed')
        self.assert_error_count(widget, max_errors)

    def test_synchronous_transition(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'cancel')
        self.assert_state(widget, 'cancelled')

    def test_background_action_does_not_change_state(self):
        widget = self.create_instance(status='fulfilled')
        self.background_transition(widget, 'sync_inventory')
        self.assert_state(widget, 'fulfilled')  # action: no target write
        self.assert_side_effects_ran(['bg_ok'])
        self.assert_callbacks_ran(['bg_callback'])

    def test_not_available_from_wrong_state(self):
        widget = self.create_instance(status='draft')
        self.assert_not_available(widget, ['generate_export'])  # needs 'fulfilled'

    def test_snapshot_capture_and_replay(self):
        """Capture a stuck instance, rebuild it from the snapshot, fix it."""
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'fulfil', fail_side_effect='bg_ok', fail_with=ValueError('snap'))

        snap = self.snapshot(widget)
        self.assertEqual(snap['state'], 'fulfilling')
        self.assertIn('transition_message', snap)

        # Remove the original so from_snapshot can recreate it with the same pk.
        TransitionMessage.objects.filter(instance_id=str(widget.pk)).delete()
        Widget.objects.filter(pk=widget.pk).delete()

        restored = self.from_snapshot(snap)
        self.assert_state(restored, 'fulfilling')
        self.assert_error_count(restored, 1)

        self.retry_transition(restored)  # the "fix" works
        self.assert_state(restored, 'fulfilled')

    def test_ai_readable_failure_output(self):
        """A failed assertion produces the structured timeline output."""
        widget = self.create_instance(status='draft')
        self.transition(widget, 'cancel')
        with self.assertRaises(AssertionError) as ctx:
            self.assert_state(widget, 'fulfilled')  # actual is 'cancelled'
        msg = str(ctx.exception)
        self.assertIn('Timeline:', msg)
        self.assertIn("Expected state 'fulfilled'", msg)
        self.assertIn('cancel', msg)  # the timeline records the cancel step


class GuardedApprovalScenario(ProcessScenario):
    process_class = ScenarioGuardProcess
    model = Widget
    state_field = 'status'
    process_name = 'guard'

    def test_permission_gate(self):
        User = get_user_model()
        staff = User.objects.create(username='sc_staff', is_staff=True)
        customer = User.objects.create(username='sc_customer', is_staff=False)

        widget = self.create_instance(status='draft')
        self.assert_available(widget, ['approve'], user=staff)
        self.assert_not_available(widget, ['approve'], user=customer)

        self.transition(widget, 'approve', user=staff)
        self.assert_state(widget, 'approved')

    def test_condition_gate(self):
        User = get_user_model()
        staff = User.objects.create(username='sc_staff2', is_staff=True)
        widget = self.create_instance(status='draft')

        widget._stock_available = False
        self.assert_not_available(widget, ['approve'], user=staff)

        widget._stock_available = True
        self.assert_available(widget, ['approve'], user=staff)
