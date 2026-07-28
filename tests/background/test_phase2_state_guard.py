"""R4 — phase-2 state guard.

Phase 2 restores the transition by name and deliberately bypasses the
source-state gate, so without a guard it would overwrite any state change
made while the row was pending (manual ops fix, external write). These
tests pin the guard's behaviour, which is unconditional since 0.10.0: the
row completes as *superseded* (``last_error_message`` starts with
``[superseded]``), side-effects are skipped, no hooks run, nothing re-raises,
and the external state change wins.

The same guard protects ``failed_state`` writes performed by the
safety-net finalizers (``runner._finalize_terminal_from_watchdog``).

Each TransitionMessage row is created directly (mirroring what phase 1
records) so phase 2 does NOT run inline and we can move the instance
out from under the pending row first.
"""
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from django_logic.background import settings as bg_settings
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import (
    finalize_stuck_attempt,
    run_background_transition,
)
from tests.background.models import Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3, TRANSITION_MESSAGE_RETRY_MINUTES=0)


def _make_tm(widget, transition_name='fulfil', queue_name='django_logic.critical',
             errors=0):
    """Create a TransitionMessage row the way phase 1 does, without
    dispatching phase 2."""
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
class EnforceModeTests(TestCase):
    """R4(a)/(b): default 'enforce' mode supersedes a pending row whose
    instance was moved by something else."""

    def test_manual_ops_fix_supersedes_pending_transition(self):
        # R4(a): phase 1 left the widget in 'fulfilling' with a pending
        # 'fulfil' row; an operator then manually fixed the widget to
        # 'cancelled'. Phase 2 must NOT run side-effects nor write target —
        # the ops fix wins and the row completes as superseded.
        widget = Widget.objects.create(status='draft')
        # What phase 1 does: TM row + in_progress_state write.
        widget.status = 'fulfilling'
        widget.save(update_fields=['status'])
        tm = _make_tm(widget, transition_name='fulfil')

        # Manual ops fix while the row is pending.
        widget.status = 'cancelled'
        widget.save(update_fields=['status'])

        # Must not raise — superseded is a clean terminal outcome.
        run_background_transition(tm.pk)

        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertTrue(tm.last_error_message.startswith('[superseded]'))
        # started_at is stamped when the attempt begins (#179), so it is
        # set even though the guard aborted; duration_ms is what stays null,
        # because no work was measured.
        self.assertIsNone(tm.duration_ms)
        # Nothing failed: errors_count untouched.
        self.assertEqual(tm.errors_count, 0)

        widget.refresh_from_db()
        # The ops fix wins; the transition's target was never written.
        self.assertEqual(widget.status, 'cancelled')
        # Side-effects skipped entirely.
        self.assertEqual(widget.se_log, '')
        # No hooks ran (neither callbacks nor failure_callbacks).
        self.assertEqual(widget.cb_log, '')

    def test_background_action_out_of_sources_is_superseded(self):
        # R4(b): a BackgroundAction has no in_progress_state, so the guard
        # checks the declared sources instead. Widget moved out of
        # sync_inventory's sources ('fulfilled'/'exported') -> superseded.
        widget = Widget.objects.create(status='fulfilled')
        tm = _make_tm(
            widget, transition_name='sync_inventory',
            queue_name='django_logic.fast',
        )
        widget.status = 'cancelled'  # moved out of the action's sources
        widget.save(update_fields=['status'])

        run_background_transition(tm.pk)

        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertTrue(tm.last_error_message.startswith('[superseded]'))
        # As above: started_at marks that an attempt began (#179); the
        # superseded row is distinguished by duration_ms staying null.
        self.assertIsNone(tm.duration_ms)

        widget.refresh_from_db()
        self.assertEqual(widget.status, 'cancelled')
        self.assertEqual(widget.se_log, '')  # side-effects skipped
        self.assertEqual(widget.cb_log, '')  # no hooks ran

    def test_background_action_still_in_sources_runs_normally(self):
        # R4(b) positive control: the widget is still in one of the
        # action's declared sources, so the guard passes and the action
        # executes normally.
        widget = Widget.objects.create(status='fulfilled')
        tm = _make_tm(
            widget, transition_name='sync_inventory',
            queue_name='django_logic.fast',
        )

        run_background_transition(tm.pk)

        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.last_error_message, '')
        self.assertIsNotNone(tm.started_at)

        widget.refresh_from_db()
        # BackgroundAction does not change state on success.
        self.assertEqual(widget.status, 'fulfilled')
        self.assertIn('ok,', widget.se_log)  # side-effects ran
        self.assertIn('cb,', widget.cb_log)  # success callbacks ran


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class SafetyNetGuardTests(TestCase):
    """R4(d): the same guard protects failed_state writes made by the
    safety-net finalizers (finalize_stuck_attempt / detect_stuck)."""

    def test_finalize_stuck_skips_failed_state_after_ops_fix(self):
        # R4(d): a row stuck at MAX_ERRORS whose widget was manually moved
        # to 'cancelled'. Finalization still completes the row (retries
        # stop) but must NOT clobber the ops fix with failed_state.
        widget = Widget.objects.create(status='fulfilling')
        tm = _make_tm(widget, transition_name='fulfil', errors=3)  # >= MAX_ERRORS
        widget.status = 'cancelled'  # manual ops fix
        widget.save(update_fields=['status'])

        with self.assertLogs('django-logic.transition', level='ERROR') as cm:
            self.assertTrue(finalize_stuck_attempt(tm.pk))
        self.assertTrue(
            any('NOT writing failed_state' in message for message in cm.output),
            cm.output,
        )

        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        widget.refresh_from_db()
        # failed_state ('fulfilment_failed') was NOT written.
        self.assertEqual(widget.status, 'cancelled')

    def test_finalize_stuck_writes_failed_state_when_state_matches(self):
        # R4(d) control: the widget still sits in the in_progress_state
        # phase 1 left behind, so finalization writes failed_state.
        widget = Widget.objects.create(status='fulfilling')
        tm = _make_tm(widget, transition_name='fulfil', errors=3)

        self.assertTrue(finalize_stuck_attempt(tm.pk))

        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilment_failed')


