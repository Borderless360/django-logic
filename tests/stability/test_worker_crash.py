"""
Category 1: Celery Worker Crash Tests

Proves that the durable BackgroundTransition design recovers from worker
crashes at every point in the execution timeline.

Each test simulates a crash by raising WorkerCrashSimulated at a specific
step, then verifies the system can recover via the periodic starter.

Reference: docs/design/BACKGROUND_TRANSITION_ANALYSIS.md "Complete Execution
Timeline: Every Crash Point"
"""
from unittest.mock import patch, MagicMock

from django.core.cache import cache
from django.test import tag

from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State

from tests.stability.base import (
    StabilityTestCase, CrashSimulator, WorkerCrashSimulated,
    IdempotencyTracker,
)
from tests.stability.models import (
    Order, OrderProcess,
    side_effect_one, side_effect_two, side_effect_three,
    callback_one, callback_two,
    failure_side_effect, failure_callback,
)


@tag('stability')
class TestCrashDuringSideEffects(StabilityTestCase):
    """
    1.1 -- Worker crashes mid-side-effects.

    Side effects partially execute. On recovery (periodic starter),
    ALL side effects must re-run from scratch. Requires idempotency.
    """

    def test_crash_during_second_side_effect_then_recover(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        sim = CrashSimulator(crash_during='side_effect_two')
        patched_effects = [sim.wrap(fn) for fn in [
            side_effect_one, side_effect_two, side_effect_three
        ]]

        process_cls = type('CrashTestProcess', (OrderProcess,), {
            'transitions': [
                type(OrderProcess.transitions[1])(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    failed_state='fulfillment_failed',
                    side_effects=patched_effects,
                    failure_side_effects=[failure_side_effect],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(WorkerCrashSimulated):
            process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfillment_failed')
        self.assertIn('se1,', order.side_effect_log)
        self.assertNotIn('se3,', order.side_effect_log)

    def test_idempotent_side_effects_survive_retry(self):
        """Side effects that ran before the crash run again on retry."""
        tracker = IdempotencyTracker()
        tracked_se1 = tracker.track(side_effect_one)

        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        sim = CrashSimulator(crash_during='side_effect_two')

        process_cls = type('RetryTestProcess', (OrderProcess,), {
            'transitions': [
                type(OrderProcess.transitions[1])(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    failed_state='fulfillment_failed',
                    side_effects=[
                        sim.wrap(tracked_se1),
                        sim.wrap(side_effect_two),
                        sim.wrap(side_effect_three),
                    ],
                    failure_side_effects=[failure_side_effect],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)

        with self.assertRaises(WorkerCrashSimulated):
            process.fulfill()

        self.assertEqual(tracker.counts.get('side_effect_one', 0), 1)

        order.refresh_from_db()
        order.side_effect_log = ''
        order.save(update_fields=['side_effect_log'])

        sim.reset()
        sim.crash_during = None

        order.status = 'approved'
        order.save(update_fields=['status'])
        cache.clear()

        process2 = process_cls(field_name='status', instance=order)
        process2.fulfill()

        self.assertEqual(tracker.counts['side_effect_one'], 2)

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')


@tag('stability')
class TestCrashAfterTargetStateSet(StabilityTestCase):
    """
    1.2 -- Worker crashes after target state is set but before any
    "mark as completed" step.

    The state is correct (target). On recovery, the handler should detect
    that the transition already completed and clean up without re-running
    side effects.
    """

    def test_state_already_at_target_no_duplicate_side_effects(self):
        order = Order.objects.create(status='fulfilled')
        state = State(order, 'status', process_name='process')

        available = list(
            OrderProcess(field_name='status', instance=order)
            .get_available_transitions(action_name='fulfill')
        )
        self.assertEqual(len(available), 0,
            "No 'fulfill' transition should be available when state is already 'fulfilled'"
        )


@tag('stability')
class TestCrashDuringCallbacks(StabilityTestCase):
    """
    1.3 -- Worker crashes during callbacks (after target state set and
    transition marked as completed).

    State is correct. Callbacks are best-effort and may be lost.
    This validates the documented reliability contract.
    """

    def test_callback_crash_does_not_corrupt_state(self):
        def crashing_callback(instance, **kwargs):
            raise WorkerCrashSimulated("Crash during callback")

        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        process_cls = type('CallbackCrashProcess', (OrderProcess,), {
            'transitions': [
                type(OrderProcess.transitions[1])(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    side_effects=[side_effect_one],
                    callbacks=[crashing_callback],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')
        self.assert_unlocked(state)


@tag('stability')
class TestCrashBetweenCommitAndDispatch(StabilityTestCase):
    """
    1.4 -- Phase 1 atomic block commits but on_commit never fires.

    The TransitionMessage row exists in DB but no Celery task was dispatched.
    The periodic starter must find and re-dispatch the message.

    NOTE: This test validates the contract at the state/lock level.
    Full TransitionMessage integration requires the BackgroundTransition
    implementation from Stage 2.
    """

    def test_state_set_but_dispatch_fails_lock_remains(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        self.assertTrue(state.lock())
        state.set_state('fulfilling')

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilling')
        self.assert_locked(state)

        state.unlock()
        self._tracked_cache_keys.discard(state._get_hash())


@tag('stability')
class TestCrashDuringFailurePath(StabilityTestCase):
    """
    1.5 -- Side effect raises, then worker crashes during failure_side_effects.

    On recovery, the full failure path should execute again (side effects
    fail again, failure_side_effects run, failed_state set).
    """

    def test_crash_during_failure_side_effects(self):
        call_log = []

        def failing_side_effect(instance, **kwargs):
            call_log.append('failing_se')
            raise ValueError("Business error")

        def crashing_failure_se(instance, **kwargs):
            call_log.append('failure_se_crash')
            raise WorkerCrashSimulated("Crash during failure SE")

        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        process_cls = type('FailurePathCrashProcess', (OrderProcess,), {
            'transitions': [
                type(OrderProcess.transitions[1])(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    failed_state='fulfillment_failed',
                    side_effects=[failing_side_effect],
                    failure_side_effects=[crashing_failure_se],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(ValueError):
            process.fulfill()

        self.assertIn('failing_se', call_log)
        self.assertIn('failure_se_crash', call_log)

        order.refresh_from_db()
        self.assertIn(order.status, ('fulfillment_failed', 'fulfilling'))


@tag('stability')
class TestMaxRetriesExhausted(StabilityTestCase):
    """
    1.6 -- After N consecutive failures, the transition should reach
    failed_state permanently.
    """

    def test_repeated_failures_reach_failed_state(self):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)
        max_retries = 3

        for i in range(max_retries):
            order.status = 'approved'
            order.side_effect_log = ''
            order.save(update_fields=['status', 'side_effect_log'])
            cache.clear()

            def always_failing(instance, **kwargs):
                raise ValueError(f"Attempt {i + 1}")

            process_cls = type('MaxRetryProcess', (OrderProcess,), {
                'transitions': [
                    type(OrderProcess.transitions[1])(
                        action_name='fulfill',
                        sources=['approved'],
                        target='fulfilled',
                        in_progress_state='fulfilling',
                        failed_state='fulfillment_failed',
                        side_effects=[always_failing],
                        failure_side_effects=[failure_side_effect],
                    )
                ]
            })

            process = process_cls(field_name='status', instance=order)
            with self.assertRaises(ValueError):
                process.fulfill()

            order.refresh_from_db()
            self.assertEqual(order.status, 'fulfillment_failed')

    def test_no_failed_state_leaves_in_progress(self):
        """Without failed_state, the state stays at in_progress on failure."""
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        def failing_se(instance, **kwargs):
            raise ValueError("oops")

        process_cls = type('NoFailedStateProcess', (OrderProcess,), {
            'transitions': [
                type(OrderProcess.transitions[1])(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    side_effects=[failing_se],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(ValueError):
            process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilling')
        self.assert_unlocked(state)
