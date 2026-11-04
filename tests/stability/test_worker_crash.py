"""Synchronous transition behavior after injected exceptions.

These tests check saved state, cache-lock release and explicit repeated calls.
An ordinary test exception is not proof of hard process-stop recovery.
Real background-worker recovery tests live in tests.background.
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
    failure_callback,
)


@tag('stability')
class TestCrashDuringSideEffects(StabilityTestCase):
    """
    A side effect raises after earlier work ran.
    An explicit repeated call runs those side effects again, so they require duplicate protection.
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
                    failed_state='fulfillment_failed',
                    side_effects=patched_effects,
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
                    failed_state='fulfillment_failed',
                    side_effects=[
                        sim.wrap(tracked_se1),
                        sim.wrap(side_effect_two),
                        sim.wrap(side_effect_three),
                    ],
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
    A record already at the target state does not offer the earlier action.
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
    An ordinary callback exception is swallowed after the target state is written.
    The synchronous call can then finish and commit that state.
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
class TestCommittedStateWithHeldCacheLock(StabilityTestCase):
    """
    A direct state write commits independently of the held cache lock.
    This test creates no background job and does not exercise worker delivery.
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
class TestMaxRetriesExhausted(StabilityTestCase):
    """
    Each explicit synchronous call records its declared failed state when its side effect raises.
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
                        failed_state='fulfillment_failed',
                        side_effects=[always_failing],
                    )
                ]
            })

            process = process_cls(field_name='status', instance=order)
            with self.assertRaises(ValueError):
                process.fulfill()

            order.refresh_from_db()
            self.assertEqual(order.status, 'fulfillment_failed')

    def test_no_failed_state_leaves_the_source_state(self):
        """Without failed_state, a failed sync run leaves the instance at its
        source with no in-progress state written — it can run again, not parked."""
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
                    side_effects=[failing_se],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(ValueError):
            process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')
        self.assert_unlocked(state)
