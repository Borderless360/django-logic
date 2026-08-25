"""Behavior-focused Transition / Transition tests.

These tests replaced an older suite that drove ``transition.change_state(state)``
directly (bypassing the ``instance.process.<action>()`` entrypoint users
actually call), mocked ``change_state`` to assert it was called, and
asserted on private helpers like ``_init_transition_context``. Those tests
re-stated the implementation and prevented nothing.

The replacements drive a real object through the real entrypoint and
assert on the observable transformation: the state the object landed in,
the ordered side-effects/callbacks that mutated it, the failure path's
``failed_state`` + cleanup, the lock discipline, and the ``next_transition``
context contract. Fixtures live in tests/background/models.py; binding in
tests/background/apps.py.
"""
from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State
from django_logic.testing import JourneyStep, ProcessScenario
from tests.background.models import (
    CALLBACK_SEEN_STATE,
    SYNC_LAST_KWARGS,
    SYNC_ORDER,
    Widget,
    WidgetAmbiguousNextProcess,
    WidgetContextProcess,
    WidgetSyncProcess,
)
from tests import dl_settings


# ProcessScenario runs in sync mode by default (BACKGROUND_EXECUTION='sync'
# is set per-class via override_settings where background work is involved;
# the sync Transition/Transition tests below don't touch the background engine,
# so the default sync setting is fine).
_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class TransitionSideEffectsScenario(ProcessScenario):
    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

    def test_side_effects_run_in_order_and_write_target(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'approve')
        self.assert_state(widget, 'notified')  # approve -> notify (next_transition)
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
            fail_side_effect='se_reject_attempt', fail_with=ValueError('reject broke'),
        )
        self.assert_state(widget, 'rejection_failed')
        self.assert_state_trace(['rejection_failed'])
        # The success side-effect did not complete; the failure hooks ran.
        self.assert_side_effects_not_ran(['se_reject_attempt'])
        self.assert_failure_callbacks_ran(['fcb_on_fail'])

    def test_lock_is_released_after_success(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'approve')
        self.assertFalse(self._process(widget).state.is_locked())

    def test_lock_is_released_after_failure(self):
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'reject',
            fail_side_effect='se_reject_attempt', fail_with=ValueError('boom'),
        )
        self.assertFalse(self._process(widget).state.is_locked())


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class ActionScenario(ProcessScenario):
    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

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
            fail_side_effect='se_poke_attempt', fail_with=ValueError('poke broke'),
        )
        self.assert_state(widget, 'poked_failed')
        self.assert_state_trace(['poked_failed'])
        self.assert_failure_callbacks_ran(['fcb_on_poke_fail'])

    def test_a_held_lock_refuses_a_no_target_transition(self):
        # A transition with no target takes the lock like any other, so a
        # concurrent holder refuses it before its side-effects run, and
        # the holder's lock survives.
        widget = self.create_instance(status='draft')
        state = self._process(widget).state
        self.assertTrue(state.lock(), 'pre-condition: acquire the lock')
        try:
            self.transition(
                widget, 'poke_fail', expect_raises=TransitionNotAllowed,
            )
            self.assertTrue(
                state.is_locked(),
                'the refused transition released a lock another holder owns',
            )
            # Nothing ran, so nothing was written.
            self.assert_state(widget, 'draft')
        finally:
            state.unlock()

    def test_failed_state_write_happens_under_the_lock(self):
        # The write must happen while the Transition itself holds the lock —
        # observed by reading the lock key at write time (#185).
        widget = self.create_instance(status='draft')
        real_set_state = State.set_state
        locked_at_write = []

        def observing(state, value):
            locked_at_write.append(cache.get(state._get_hash()) is not None)
            real_set_state(state, value)

        with mock.patch.object(State, 'set_state', autospec=True,
                               side_effect=observing):
            self.transition(
                widget, 'poke_fail',
                fail_side_effect='se_poke_attempt', fail_with=ValueError('poke broke'),
            )
        self.assert_state(widget, 'poked_failed')
        self.assertEqual(
            locked_at_write, [True],
            'failed_state was written without holding the state lock',
        )

    def test_concurrent_transition_cannot_start_during_failed_state_write(self):
        # The #185 TOCTOU: is_locked()-then-write left a window where a
        # concurrent Transition could acquire the lock and the Transition's stale
        # write then clobbered that flight's state. The atomic acquire closes
        # it — a rival lock() attempt mid-write must lose.
        widget = self.create_instance(status='draft')
        real_set_state = State.set_state
        rival_lock_results = []

        def racing(state, value):
            rival = State(state.instance, state.field_name,
                          process_name=state.process_name)
            acquired = rival.lock()
            rival_lock_results.append(acquired)
            if acquired:
                # Only reachable when the fix regressed; keep the shared
                # cache clean for the rest of the suite.
                rival.unlock()
            real_set_state(state, value)

        with mock.patch.object(State, 'set_state', autospec=True,
                               side_effect=racing):
            self.transition(
                widget, 'poke_fail',
                fail_side_effect='se_poke_attempt', fail_with=ValueError('poke broke'),
            )
        self.assertEqual(
            rival_lock_results, [False],
            'a concurrent transition could start during the failed_state write',
        )
        self.assert_state(widget, 'poked_failed')

    def test_lock_released_after_failed_state_write(self):
        # The write-scoped lock (#185) must not outlive the write.
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'poke_fail',
            fail_side_effect='se_poke_attempt', fail_with=ValueError('poke broke'),
        )
        self.assert_state(widget, 'poked_failed')
        self.assertFalse(self._process(widget).state.is_locked())

    def test_lock_released_when_failed_state_write_raises(self):
        # A rejected failed_state write must not replace the original
        # side-effect exception (#178) nor leak the write-scoped lock (#185).
        widget = self.create_instance(status='draft')
        with mock.patch.object(State, 'set_state', autospec=True,
                               side_effect=RuntimeError('db refused')):
            self.transition(
                widget, 'poke_fail',
                fail_side_effect='se_poke_attempt', fail_with=ValueError('poke broke'),
                expect_raises=ValueError,
            )
        self.assert_raised(ValueError, match='poke broke')
        self.assert_failure_callbacks_ran(['fcb_on_poke_fail'])
        self.assertFalse(self._process(widget).state.is_locked())
        # The rejected write never landed — the object stays put.
        self.assert_state(widget, 'draft')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class KwargsAndFailureContractScenario(ProcessScenario):
    """The kwargs + ``exception`` contract side-effects/callbacks receive."""

    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

    def test_kwargs_forwarded_to_side_effects(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'capture', foo='bar', amount=42)
        self.assertEqual(SYNC_LAST_KWARGS.get('foo'), 'bar')
        self.assertEqual(SYNC_LAST_KWARGS.get('amount'), 42)

    def test_failure_callback_receives_exception_and_forwarded_kwargs(self):
        widget = self.create_instance(status='draft')
        self.transition(
            widget, 'capture_fail',
            fail_side_effect='sync_boom', fail_with=ValueError('captured boom'),
            foo='bar',
        )
        self.assert_state(widget, 'capture_failed')
        # The failure callback got the original exception + the caller's kwarg.
        self.assertIsInstance(SYNC_LAST_KWARGS.get('exception'), ValueError)
        self.assertIn('captured boom', str(SYNC_LAST_KWARGS['exception']))
        self.assertEqual(SYNC_LAST_KWARGS.get('foo'), 'bar')

@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class NextTransitionScenario(ProcessScenario):
    """``next_transition`` behavior — the follow-up runs through the entrypoint
    with a fresh ``tr_id`` and chained ``root_id``/``parent_id``, only on
    success, and is refused when ambiguous."""

    process_class = WidgetContextProcess
    model = Widget
    state_field = 'status'
    process_name = 'ctx_proc'

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

    def test_journey_pins_the_chain(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'parent_act', root_id='ROOT')
        self.assert_journey([
            JourneyStep(
                action='parent_act',
                before='draft',
                after='child_done',
                side_effects=['se_parent', 'sync_capture'],
                callbacks=[],
                failed=False,
            ),
        ])


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class NextTransitionFailureScenario(ProcessScenario):
    """``next_transition`` must NOT fire when the parent transition fails."""

    process_class = WidgetSyncProcess
    model = Widget
    state_field = 'status'
    process_name = 'sync_proc'

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
class AmbiguousNextTransitionScenario(ProcessScenario):
    """An ambiguous ``next_transition`` (two same-name follow-ups both
    available, no disambiguating condition) is refused — neither runs —
    rather than picking arbitrarily. The parent still completes.

    Note: this pins the observable BEHAVIOUR ('runs neither'), which is
    enforced by two independent layers — ``NextTransition.execute``'s own
    ambiguity guard and the entrypoint's ``_resolve_transition_with_owner``
    refusal (whose exception NextTransition swallows). It therefore does not
    isolate either guard on its own; removing just one still leaves the object
    in ``started``. That defence-in-depth is intended."""

    process_class = WidgetAmbiguousNextProcess
    model = Widget
    state_field = 'status'
    process_name = 'ambig_next'

    def test_ambiguous_follow_up_runs_neither(self):
        widget = self.create_instance(status='draft')
        self.transition(widget, 'start')
        # 'start' completed; the ambiguous follow-up was refused.
        self.assert_state(widget, 'started')
        self.assert_side_effects_ran(['se_start'])
        self.assert_side_effects_not_ran(['se_follow_a', 'se_follow_b'])
        self.assert_state_trace(['started'])  # no follow-up state write
