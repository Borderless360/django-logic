"""Behavior-focused synchronous transition tests.

These tests replaced an older suite that drove ``transition.change_state(state)``
directly (bypassing the ``instance.process.<action>()`` entrypoint users
actually call), mocked ``change_state`` to assert it was called, and
asserted on private helpers like ``_init_transition_context``. Those tests
re-stated the implementation and prevented nothing.

The replacements drive a real object through the real entrypoint and
assert on the observable transformation: the state the object landed in,
the ordered side-effects/callbacks that mutated it, the failure path's
``failed_state`` + cleanup, the lock discipline, and the callback
context contract. Fixtures live in tests/background/models.py; binding in
tests/background/apps.py.
"""
from tests import StateTraceMixin

from django.test import override_settings

from django_logic.testing import ProcessScenario
from tests.background.models import (
    CALLBACK_SEEN_STATE,
    SYNC_LAST_KWARGS,
    Widget,
    WidgetAmbiguousCallbackProcess,
    WidgetContextProcess,
    WidgetSyncProcess,
)
from tests import dl_settings


# ProcessScenario runs in sync mode by default (BACKGROUND_EXECUTION='sync'
# is set per-class via override_settings where background work is involved;
# the synchronous transition tests below don't touch the background engine,
# so the default sync setting is fine).
_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class TransitionSideEffectsScenario(StateTraceMixin, ProcessScenario):
    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'

    def test_side_effects_run_in_order_and_write_target(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'approve')
        self.assert_state(widget, 'notified')  # approve -> notify (callback)
        self.assert_side_effects_ran(['se_a', 'se_b', 'se_c'])
        widget.refresh_from_db()
        # se_log records the declaration order verbatim.
        self.assertEqual(widget.se_log, 'a,b,c,')

    def test_callback_runs_after_target_is_written(self):
        # Ordering is made OBSERVABLE: 'finalize' has a callback that reads the
        # persisted state at call time. If the target is written before
        # callbacks run (the contract), the callback sees 'finalized'. A
        # regression that runs callbacks before the state write would record
        # 'draft' here and fail — the previous version of this test could not
        # tell the difference.
        CALLBACK_SEEN_STATE.clear()
        widget = self.create_instance(status='draft')
        self.transition(widget, 'finalize')
        self.assert_state(widget, 'finalized')
        self.assert_callbacks_ran(['cb_record_seen_state'])
        self.assertEqual(CALLBACK_SEEN_STATE, ['finalized'])
        widget.refresh_from_db()
        self.assertIn('seen_state,', widget.cb_log)

    def test_callback_exception_is_swallowed_and_target_kept(self):
        # A raising callback is best-effort: the target state survives and
        # the exception does not propagate out of the entrypoint.
        widget = self.create_instance(status='draft')
        # 'boom_callback' has a callback that raises; the drive must not
        # surface it (Callbacks.execute swallows).
        self.transition(widget, 'boom_callback')
        self.assert_state(widget, 'boom_done')
        self.assert_state_trace(['boom_done'])

    def test_failure_during_side_effect_writes_failed_state(self):
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'reject',
            expect_raises=ValueError, fail_side_effect='se_reject_attempt', fail_with=ValueError('reject broke'),
        )
        self.assert_state(widget, 'rejection_failed')
        self.assert_state_trace(['rejection_failed'])
        # The success side-effect did not complete; the failure hooks ran.
        self.assert_side_effects_not_ran(['se_reject_attempt'])
        self.assert_failure_callbacks_ran(['fcb_on_fail'])


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class ActionScenario(StateTraceMixin, ProcessScenario):
    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'

    def test_action_runs_side_effects_without_changing_state(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'poke')
        self.assert_state(widget, 'draft')
        self.assert_state_trace([])  # no target: nothing writes state on success
        self.assert_side_effects_ran(['se_poke'])
        self.assert_callbacks_ran(['cb_after_poke'])

    def test_action_failure_writes_failed_state_when_unlocked(self):
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'poke_fail',
            expect_raises=ValueError, fail_side_effect='se_poke_attempt', fail_with=ValueError('poke broke'),
        )
        self.assert_state(widget, 'poked_failed')
        self.assert_state_trace(['poked_failed'])
        self.assert_failure_callbacks_ran(['fcb_on_poke_fail'])


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class KwargsAndFailureContractScenario(StateTraceMixin, ProcessScenario):
    """The kwargs + ``exception`` contract side-effects/callbacks receive."""

    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'

    def test_kwargs_forwarded_to_side_effects(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'capture', foo='bar', amount=42)
        self.assertEqual(SYNC_LAST_KWARGS.get('foo'), 'bar')
        self.assertEqual(SYNC_LAST_KWARGS.get('amount'), 42)

    def test_failure_callback_receives_exception_and_forwarded_kwargs(self):
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'capture_fail',
            expect_raises=ValueError, fail_side_effect='sync_boom', fail_with=ValueError('captured boom'),
            foo='bar',
        )
        self.assert_state(widget, 'capture_failed')
        # The failure callback got the original exception + the caller's kwarg.
        self.assertIsInstance(SYNC_LAST_KWARGS.get('exception'), ValueError)
        self.assertIn('captured boom', str(SYNC_LAST_KWARGS['exception']))
        self.assertEqual(SYNC_LAST_KWARGS.get('foo'), 'bar')

@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class CallbackTransitionScenario(StateTraceMixin, ProcessScenario):
    """Callback follow-up behavior — the follow-up runs through the entrypoint
    with a fresh ``tr_id`` and chained ``root_id``/``parent_id``, only on
    success, and is refused when ambiguous."""

    process_class = WidgetContextProcess
    model = Widget
    state_field = 'status'

    def test_follow_up_runs_with_fresh_tr_id_and_chained_context(self):
        widget = self.create_instance(status='draft')
        # Drive the parent through the entrypoint with a caller-supplied
        # root_id. The follow-up (child_act) captures its kwargs.
        self.transition(widget, 'parent_act', root_id='ROOT')
        # The whole chain ran: parent_done -> child_done.
        self.assert_state(widget, 'child_done')
        self.assert_state_trace(['parent_done', 'child_done'])
        self.assert_side_effects_ran(['se_parent', 'sync_capture'])

        # The follow-up got its OWN tr_id (not the parent's), the root_id
        # chained from the caller, and parent_id links to the parent's tr_id.
        captured = SYNC_LAST_KWARGS
        self.assertEqual(captured.get('root_id'), 'ROOT')
        self.assertIsNotNone(captured.get('tr_id'))
        self.assertIsNotNone(captured.get('parent_id'))
        self.assertNotEqual(captured.get('tr_id'), captured.get('parent_id'))
        # parent_id is the parent's tr_id, not the root.
        self.assertNotEqual(captured.get('parent_id'), 'ROOT')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class CallbackTransitionFailureScenario(StateTraceMixin, ProcessScenario):
    """Success callbacks must NOT run when the parent transition fails."""

    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'

    def test_follow_up_skipped_when_parent_fails(self):
        # 'approve' chains into 'notify' on success. Inject a failure on
        # approve's first side-effect: the parent fails and re-raises to the
        # caller (approve is the driven transition), the follow-up never fires,
        # and — approve has no failed_state — the object stays in 'draft'.
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'approve',
            fail_side_effect='se_a', fail_with=ValueError('approve boom'),
            expect_raises=ValueError,
        )
        # Landed back in the source state (no failed_state, no state write).
        self.assert_state(widget, 'draft')
        self.assert_state_trace([])
        # se_b (approve's own, after the failed se_a) and se_c (notify's
        # follow-up) never ran — the whole chain stopped at the failure. (se_a
        # is the injected target and never records regardless, so it carries
        # no signal here and is deliberately not asserted.)
        self.assert_side_effects_not_ran(['se_b', 'se_c'])


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class AmbiguousCallbackTransitionScenario(StateTraceMixin, ProcessScenario):
    """A callback cannot choose between two available follow-up transitions.

    The process entrypoint refuses the ambiguous action. The callback contains
    that refusal, so the completed parent keeps its target state.
    """

    process_class = WidgetAmbiguousCallbackProcess
    model = Widget
    state_field = 'status'

    def test_ambiguous_follow_up_runs_neither(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'start')
        # 'start' completed; the ambiguous follow-up was refused.
        self.assert_state(widget, 'started')
        self.assert_side_effects_ran(['se_start'])
        self.assert_side_effects_not_ran(['se_follow_a', 'se_follow_b'])
        self.assert_state_trace(['started'])  # no follow-up state write
