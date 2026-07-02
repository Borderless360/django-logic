"""Behavior-focused Process tests.

Every test here drives a real object through the real ``instance.process.<action>()``
entrypoint and asserts on the OBSERVABLE transformation — what state the
object landed in, which side-effects/callbacks mutated it, what became
available next, and how the object moved through the workflow. No test
defines a process inline and asserts on framework return values, and
nothing mocks ``change_state``: those tests just re-state the
implementation and prevent nothing (a rewrite regenerates them to match
whatever code exists).

The fixtures live in tests/background/models.py and are bound in
tests/background/apps.py (the single binding site, per issue #100).
"""
from django.contrib.auth import get_user_model

from django_logic.exceptions import TransitionNotAllowed
from django_logic.testing import JourneyStep, ProcessScenario
from tests.background.models import (
    Widget,
    WidgetAmbiguousConditionProcess,
    WidgetNestedSyncProcess,
    WidgetSyncProcess,
)


class SyncProcessAvailabilityScenario(ProcessScenario):
    """What an instance MAY do from a given state — asserted as availability,
    not as the framework's internal transition list."""

    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

    def test_actions_available_from_draft(self):
        widget = self.create_instance(status='draft')
        # All draft-sourced actions are available from 'draft'.
        self.assert_available(
            widget,
            ['approve', 'reject', 'poke', 'poke_fail',
             'cancel', 'staff_only', 'capture', 'capture_fail', 'boom_callback'],
        )

    def test_actions_not_available_from_wrong_state(self):
        widget = self.create_instance(status='approved')
        # From 'approved' only the follow-up 'notify' is available — the
        # draft-only actions are gated by source state.
        self.assert_available(widget, ['notify'])
        self.assert_not_available(
            widget,
            ['approve', 'reject', 'poke', 'cancel', 'staff_only'],
        )

    def test_nothing_available_from_terminal_state(self):
        widget = self.create_instance(status='notified')
        self.assertEqual(self._available(widget), [])

    def test_condition_routes_same_action_name(self):
        # Two 'cancel' transitions share an action_name, disambiguated by a
        # condition on kwargs_seen. The object's DESTINATION depends on the
        # condition — that is the observable behavior to pin.
        widget = self.create_instance(status='draft')
        self.transition(widget, 'cancel')
        self.assert_state(widget, 'cancelled')
        self.assertIn('cancel_plain,', widget.se_log)
        self.assertNotIn('cancel_flagged,', widget.se_log)

        flagged = self.create_instance(status='draft', kwargs_seen=['flag'])
        self.transition(flagged, 'cancel')
        self.assert_state(flagged, 'archived')
        self.assertIn('cancel_flagged,', flagged.se_log)

    def test_permission_gate_blocks_non_staff(self):
        User = get_user_model()
        staff = User.objects.create(username='proc_staff', is_staff=True)
        customer = User.objects.create(username='proc_customer', is_staff=False)

        widget = self.create_instance(status='draft')
        self.assert_available(widget, ['staff_only'], user=staff)
        self.assert_not_available(widget, ['staff_only'], user=customer)

        self.transition(widget, 'staff_only', user=staff)
        self.assert_state(widget, 'staffed')
        self.assertIn('staff,', widget.se_log)

    def test_permission_denial_keeps_object_unchanged(self):
        User = get_user_model()
        customer = User.objects.create(username='proc_customer2', is_staff=False)
        widget = self.create_instance(status='draft')
        # Drive through the real entrypoint; permission denial raises at
        # resolve time, before any state write or side-effect.
        with self.assertRaises(TransitionNotAllowed):
            self._process(widget).staff_only(user=customer)
        # Object untouched: state unchanged, side-effect never ran.
        widget.refresh_from_db()
        self.assertEqual(widget.status, 'draft')
        self.assertNotIn('staff,', widget.se_log)


class SyncProcessDrivingScenario(ProcessScenario):
    """How the object changes as it is driven through actions."""

    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

    def test_approve_chains_into_notify_via_next_transition(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'approve')
        # The object passed through both states; the follow-up's side-effect
        # ran in the same drive.
        self.assert_state_trace(['approved', 'notified'])
        self.assert_state(widget, 'notified')
        self.assert_side_effects_ran(['se_a', 'se_b', 'se_c'])
        self.assert_callbacks_ran(['cb_after_approve'])

    def test_side_effects_run_in_declaration_order(self):
        # se_a is declared before se_b; the recorded order reflects the
        # declaration order, which is the contract callers rely on.
        widget = self.create_instance(status='draft')
        self.transition(widget, 'approve')
        ran = self._tracker().side_effects_ran
        self.assertEqual(ran.index('se_a') < ran.index('se_b'), True)
        # And the object's se_log reflects that order.
        widget.refresh_from_db()
        self.assertEqual(widget.se_log, 'a,b,c,')

    def test_reject_failure_path(self):
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'reject',
            fail_side_effect='se_reject_attempt', fail_with=ValueError('reject broke'),
        )
        # Failure writes failed_state; the failure hooks ran; the success
        # side-effect did NOT complete.
        self.assert_state(widget, 'rejection_failed')
        self.assert_state_trace(['rejection_failed'])
        self.assert_failure_side_effects_ran(['fse_cleanup'])
        self.assert_failure_callbacks_ran(['fcb_on_fail'])
        self.assert_side_effects_not_ran(['se_reject_attempt'])

    def test_failure_side_effects_run_before_failure_callbacks(self):
        # The cross-hook ordering is observable via SYNC_ORDER.
        from tests.background.models import SYNC_ORDER
        SYNC_ORDER.clear()
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'reject',
            fail_side_effect='se_reject_attempt', fail_with=ValueError('boom'),
        )
        self.assertEqual(SYNC_ORDER, ['fse:cleanup', 'fcb:on_fail'])

    def test_action_does_not_change_state_on_success(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'poke')
        self.assert_state(widget, 'draft')
        self.assert_state_trace([])  # no state write at all
        self.assert_side_effects_ran(['se_poke'])
        self.assert_callbacks_ran(['cb_after_poke'])

    def test_action_failed_state_only_written_when_unlocked(self):
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'poke_fail',
            fail_side_effect='se_poke_attempt', fail_with=ValueError('poke broke'),
        )
        self.assert_state(widget, 'poked_failed')
        self.assert_failure_callbacks_ran(['fcb_on_poke_fail'])

    def test_failing_action_does_not_release_a_concurrent_lock(self):
        # An Action never acquires the state lock; a failing Action must not
        # release one a concurrent Transition legitimately holds. Drive
        # through the entrypoint with a pre-held lock and assert it survives.
        widget = self.create_instance(status='draft')
        state = self._process(widget).state
        self.assertTrue(state.lock(), 'pre-condition: acquire the lock')
        try:
            self.transition(
                widget, 'poke_fail',
                fail_side_effect='se_poke_attempt', fail_with=ValueError('poke broke'),
            )
            self.assertTrue(
                state.is_locked(),
                'failing Action released a lock it never acquired',
            )
            # failed_state is skipped while locked — the object stays put.
            self.assert_state(widget, 'draft')
        finally:
            state.unlock()

    def test_kwargs_are_forwarded_to_side_effects(self):
        from tests.background.models import SYNC_LAST_KWARGS
        widget = self.create_instance(status='draft')
        self.transition(widget, 'capture', foo='bar')
        # The kwarg reached the side-effect...
        self.assertEqual(SYNC_LAST_KWARGS.get('foo'), 'bar')
        # ...and the drive actually transformed the persisted object (guardrail
        # c: a kwargs check alone would pass even if the engine never ran).
        self.assert_state(widget, 'captured')
        self.assertIn('captured,', widget.se_log)

    def test_positional_arguments_are_rejected(self):
        # A positional user used to be silently dropped, bypassing permission
        # checks. The entrypoint must refuse it loudly.
        widget = self.create_instance(status='draft')
        with self.assertRaises(TypeError):
            # Pass a positional argument to the action method.
            self._process(widget).approve('not-a-kwarg')

    def test_journey_pins_the_whole_approve_workflow(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'approve')
        self.assert_journey([
            JourneyStep(
                action='approve',
                before='draft',
                after='notified',
                side_effects=['se_a', 'se_b', 'se_c'],
                callbacks=['cb_after_approve'],
                failed=False,
            ),
        ])


class NestedSyncDelegationScenario(ProcessScenario):
    """A parent process drives a transition declared on a nested process,
    through the parent entrypoint — the observable behavior the old
    ``test_nested_process_*`` tests expressed as transition-object identity."""

    process_class = WidgetNestedSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'nested_sync'

    def test_parent_entrypoint_drives_nested_transition(self):
        widget = self.create_instance(status='draft')
        # 'inner_act' is declared on InnerSyncProcess, not on the bound parent.
        # The parent entrypoint must still find and run it.
        self.assert_available(widget, ['inner_act'])
        self.transition(widget, 'inner_act')
        self.assert_state(widget, 'inner_done')
        self.assert_side_effects_ran(['se_inner'])


class AmbiguousConditionScenario(ProcessScenario):
    """Two transitions share the action_name 'clash' and BOTH conditions pass.
    Resolution must refuse (raise TransitionNotAllowed) with no state write and
    no side-effect — it must not silently pick whichever comes first."""

    process_class = WidgetAmbiguousConditionProcess
    model = Widget
    state_field = 'status'
    process_name = 'ambig_cond'

    def test_genuinely_ambiguous_call_is_refused_with_no_state_change(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'clash', expect_raises=TransitionNotAllowed)
        # Neither destination was taken, neither side-effect ran.
        self.assert_state(widget, 'draft')
        self.assert_state_trace([])
        self.assert_side_effects_not_ran(['se_clash_a', 'se_clash_b'])
        widget.refresh_from_db()
        self.assertEqual(widget.se_log, '')


class DomainOutcomeScenario(ProcessScenario):
    """Assert what the object BECAME, not just that a hook ran (issue #103).
    ``assert_side_effects_ran`` is a wiring check; ``capture`` +
    ``assert_changed`` / ``assert_unchanged`` pin the before/after delta."""

    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

    def test_capture_and_assert_the_domain_delta(self):
        widget = self.create_instance(status='draft')
        before = self.capture(widget, ['status', 'se_log', 'audit_status'])

        self.transition(widget, 'approve')

        # The side-effect ran (wiring) AND produced the exact domain change:
        # status moved and se_log gained the ordered markers.
        self.assert_side_effects_ran(['se_a', 'se_b', 'se_c'])
        self.assert_changed(widget, before, {
            'status': ('draft', 'notified'),
            'se_log': ('', 'a,b,c,'),
        })
        # approve drives the 'sync_proc' machine; it must NOT touch the
        # sibling audit machine's field. assert_unchanged catches a hook that
        # wrote where it shouldn't.
        self.assert_unchanged(widget, before, ['audit_status'])

    def test_refused_transition_leaves_business_fields_unchanged(self):
        User = get_user_model()
        customer = User.objects.create(username='do_customer', is_staff=False)
        widget = self.create_instance(status='draft')
        before = self.capture(widget, ['status', 'se_log'])

        # A permission-denied transition is refused at resolve time. The
        # business field must be untouched — this would FAIL if the engine ran
        # the side-effect (se_staff) before denying, which is the real regression
        # assert_unchanged guards against here.
        self.transition(widget, 'staff_only', user=customer,
                        expect_raises=TransitionNotAllowed)
        self.assert_unchanged(widget, before, ['status', 'se_log'])
        self.assert_side_effects_not_ran(['se_staff'])
