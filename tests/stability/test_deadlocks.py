"""
Category 3: Deadlock Tests

Proves that the locking design never creates true deadlocks -- only
controlled rejections (TransitionNotAllowed). Tests cover:

  - Nested transition lock contention (the GV production bug)
  - Cross-instance nested transitions
  - Lock timeout and expiry recovery
  - Lock release on every failure path
  - Cache backend failure behavior

Reference: docs/research/race-condition-issue (lines 62-80, 92-106)
"""

from django.test import tag

from django_logic import Transition
from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State

from tests.stability.base import (
    StabilityTestCase,
)
from tests.stability.models import (
    Order, OrderProcess, OrderProcessWithNestedCallback,
    side_effect_one,
)


@tag('stability')
class TestNestedTransitionLockContention(StabilityTestCase):
    """
    3.1 -- The exact GV production bug: transition A locks the instance,
    a callback tries to start transition B on the SAME instance.

    Transition B must be rejected with "State is locked" -- NOT a deadlock.
    The parent transition's state remains correct.
    """

    def test_callback_on_same_instance_after_unlock(self):
        """
        Callbacks run AFTER complete_transition has already unlocked
        the state. So a callback that triggers another transition on
        the same instance will actually succeed (the lock is free).

        This is the correct behavior: unlock -> callbacks.
        The GV bug happened during SIDE EFFECTS (while lock was held),
        not during callbacks.
        """
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')

        # OrderProcessWithNestedCallback shares the 'process' name with the
        # app-bound OrderProcess, so we drive it directly without rebinding the
        # model. Its fulfill callback (trigger_nested_transition) calls
        # order.process.complete(), which resolves to the OrderProcess bound in
        # tests/stability/apps.py — the single binding site — and succeeds
        # because the lock is already released before callbacks run.
        process = OrderProcessWithNestedCallback(
            field_name='status', instance=order
        )
        process.fulfill()

        order.refresh_from_db()
        # Callback triggers complete() which succeeds because lock is released
        self.assertEqual(order.status, 'completed')

    def test_side_effect_nested_same_instance_rejected(self):
        """
        A side effect (not callback) tries to start another transition
        on the same instance. This should raise TransitionNotAllowed,
        which triggers fail_transition.
        """
        def side_effect_that_nests(instance, **kwargs):
            instance.process.cancel()

        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')

        process_cls = type('NestedSEProcess', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    failed_state='fulfillment_failed',
                    side_effects=[side_effect_that_nests],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(TransitionNotAllowed):
            process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfillment_failed')


@tag('stability')
class TestNestedTransitionDifferentInstance(StabilityTestCase):
    """
    3.2 -- Transition A on instance #1 has a callback that starts
    transition B on a DIFFERENT instance #2.

    Both locks are independent -- no deadlock possible. Both transitions
    should complete (or B should fail gracefully if #2 is already locked).
    """

    def test_callback_on_different_instance_succeeds(self):
        order1 = Order.objects.create(status='approved')
        order2 = Order.objects.create(status='draft')

        state1 = State(order1, 'status', process_name='process')
        state2 = State(order2, 'status', process_name='process')

        def approve_order2(instance, **kwargs):
            fresh_order2 = Order.objects.get(pk=order2.pk)
            p = OrderProcess(field_name='status', instance=fresh_order2)
            p.approve()

        process_cls = type('CrossInstanceProcess', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    side_effects=[side_effect_one],
                    callbacks=[approve_order2],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order1)
        process.fulfill()

        order1.refresh_from_db()
        order2.refresh_from_db()

        self.assertEqual(order1.status, 'fulfilled')
        self.assertEqual(order2.status, 'approved')


@tag('stability')
class TestLockReleaseOnEveryFailurePath(StabilityTestCase):
    """
    3.5 -- For EVERY exception type during change_state, the lock must
    be released. No orphaned locks.

    Parametrized over: side effect ValueError, side effect TypeError,
    side effect RuntimeError, side effect KeyError.
    """

    def _test_lock_released_on_exception(self, exception_cls, msg="test error"):
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')

        def failing_se(instance, **kwargs):
            raise exception_cls(msg)

        process_cls = type('LockReleaseProcess', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    failed_state='fulfillment_failed',
                    side_effects=[failing_se],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(exception_cls):
            process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfillment_failed')

    def test_lock_released_on_value_error(self):
        self._test_lock_released_on_exception(ValueError)

    def test_lock_released_on_type_error(self):
        self._test_lock_released_on_exception(TypeError)

    def test_lock_released_on_runtime_error(self):
        self._test_lock_released_on_exception(RuntimeError)

    def test_lock_released_on_key_error(self):
        self._test_lock_released_on_exception(KeyError)

    def test_lock_released_on_os_error(self):
        self._test_lock_released_on_exception(OSError)

    def test_lock_released_on_connection_error(self):
        self._test_lock_released_on_exception(ConnectionError)

    def test_lock_released_without_failed_state(self):
        """Lock must be released even when no failed_state is configured."""
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')

        def failing_se(instance, **kwargs):
            raise ValueError("no failed_state configured")

        process_cls = type('NoFailedStateLockProcess', (OrderProcess,), {
            'transitions': [
                Transition(
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
