"""The worker's source-state guard.

The worker restores the transition by name and skips the source-state gate.
Without a guard it would overwrite a state change made while the row was
pending — an operator's manual fix, or an external write. Instead the row
completes as superseded: ``last_error_message`` starts with ``[superseded]``,
side-effects are skipped, no hooks run, nothing re-raises, and the external
change wins. The safety-net finalizers guard their ``failed_state`` write the
same way.

Each test creates the TransitionMessage row directly, the way enqueue records
it. Execute then does not run inline, so the test can move the instance out
from under the pending row first.
"""
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from django_logic import conf as bg_settings
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import (
    finalize_stuck_attempt,
    run_background_transition,
)
from tests.background.models import Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3, TRANSITION_MESSAGE_RETRY_MINUTES=0)


def _make_transition_message(widget, transition_name='fulfil',
                             queue_name='django_logic.critical', errors=0):
    """Create the row enqueue would create, without sending it to a worker."""
    return TransitionMessage.objects.create(
        app_label='bg_tests',
        model_name='widget',
        instance_id=str(widget.pk),
        process_name='process',
        transition_name=transition_name,
        queue_name=queue_name,
        field_name='status',
        kwargs={},
        errors_count=errors,
    )


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class SupersededTests(TestCase):
    """The worker supersedes a pending row whose instance was moved by
    something else."""

    def test_manual_fix_supersedes_pending_transition(self):
        # Enqueue left the widget in 'fulfilling' with a pending 'fulfil' row,
        # then an operator moved the widget to 'cancelled' by hand. The worker
        # must run no side-effects and write no target: the manual fix wins and
        # the row completes as superseded.
        widget = Widget.objects.create(status='draft')
        # What enqueue writes: the in_progress_state and the row.
        widget.status = 'fulfilling'
        widget.save(update_fields=['status'])
        transition_message = _make_transition_message(
            widget, transition_name='fulfil')

        # The operator fixes the state while the row is pending.
        widget.status = 'cancelled'
        widget.save(update_fields=['status'])

        # Must not raise — superseded is a clean terminal outcome.
        run_background_transition(transition_message.pk)

        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertTrue(
            transition_message.last_error_message.startswith('[superseded]'))
        # started_at is stamped when the attempt begins, so it is set even
        # though the guard stopped the attempt. duration_ms stays null because
        # no work ran.
        self.assertIsNone(transition_message.duration_ms)
        # Nothing failed: errors_count untouched.
        self.assertEqual(transition_message.errors_count, 0)

        widget.refresh_from_db()
        # The manual fix wins; the transition's target was never written.
        self.assertEqual(widget.status, 'cancelled')
        # Side-effects skipped entirely.
        self.assertEqual(widget.se_log, '')
        # No hooks ran (neither callbacks nor failure_callbacks).
        self.assertEqual(widget.cb_log, '')

    def test_background_action_out_of_sources_is_superseded(self):
        # A BackgroundTransition has no in_progress_state, so the guard checks the
        # declared sources instead. Here the widget left sync_inventory's
        # sources ('fulfilled'/'exported').
        widget = Widget.objects.create(status='fulfilled')
        transition_message = _make_transition_message(
            widget, transition_name='sync_inventory',
            queue_name='django_logic.fast',
        )
        widget.status = 'cancelled'  # moved out of the action's sources
        widget.save(update_fields=['status'])

        run_background_transition(transition_message.pk)

        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertTrue(
            transition_message.last_error_message.startswith('[superseded]'))
        # As above: started_at marks that an attempt began, and a null
        # duration_ms is what marks the superseded row.
        self.assertIsNone(transition_message.duration_ms)

        widget.refresh_from_db()
        self.assertEqual(widget.status, 'cancelled')
        self.assertEqual(widget.se_log, '')  # side-effects skipped
        self.assertEqual(widget.cb_log, '')  # no hooks ran

    def test_background_action_still_in_sources_runs_normally(self):
        # Control: the widget is still in one of the action's declared
        # sources, so the guard passes and the action runs.
        widget = Widget.objects.create(status='fulfilled')
        transition_message = _make_transition_message(
            widget, transition_name='sync_inventory',
            queue_name='django_logic.fast',
        )

        run_background_transition(transition_message.pk)

        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.last_error_message, '')
        self.assertIsNotNone(transition_message.started_at)

        widget.refresh_from_db()
        # BackgroundTransition does not change state on success.
        self.assertEqual(widget.status, 'fulfilled')
        self.assertIn('ok,', widget.se_log)  # side-effects ran
        self.assertIn('cb,', widget.cb_log)  # success callbacks ran


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class SafetyNetGuardTests(TestCase):
    """The same guard protects the failed_state writes made by the safety-net
    finalizers."""

    def test_finalize_stuck_supersedes_after_manual_fix(self):
        # The row sits at MAX_ERRORS and an operator moved the widget to
        # 'cancelled'. Finalizing still completes the row so retries stop, but
        # the manual fix wins whole: no failed_state write, and no failure
        # hooks either. Guarding only the write left the safety net running
        # failure hooks against an instance the operator had already resolved,
        # and completing the row with nothing to explain why.
        widget = Widget.objects.create(status='fulfilling')
        transition_message = _make_transition_message(
            widget, transition_name='fulfil', errors=3)  # >= MAX_ERRORS
        transition_message.record_error(ValueError('the original cause'))
        widget.status = 'cancelled'  # the operator's manual fix
        widget.save(update_fields=['status'])

        with self.assertLogs('django-logic.transition', level='ERROR') as cm:
            self.assertTrue(finalize_stuck_attempt(transition_message.pk))
        self.assertTrue(
            any('[superseded]' in message for message in cm.output), cm.output,
        )

        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertTrue(
            transition_message.last_error_message.startswith('[superseded]'))
        # The cause that used up the retries stays readable on the row.
        self.assertIn('the original cause',
                      transition_message.last_error_message)
        widget.refresh_from_db()
        # failed_state ('fulfilment_failed') was not written...
        self.assertEqual(widget.status, 'cancelled')
        # ...and no side-effect or callback ran on the fixed instance.
        self.assertEqual(widget.se_log, '')
        self.assertEqual(widget.cb_log, '')

    def test_finalize_stuck_writes_failed_state_when_state_matches(self):
        # Control: the widget still sits in the in_progress_state enqueue
        # wrote, so finalizing writes failed_state.
        widget = Widget.objects.create(status='fulfilling')
        transition_message = _make_transition_message(
            widget, transition_name='fulfil', errors=3)

        self.assertTrue(finalize_stuck_attempt(transition_message.pk))

        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilment_failed')
