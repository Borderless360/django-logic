"""
Category 4.2, 4.6, 4.7: Transaction Integration Tests

Tests the interaction between django-logic transitions and Django's
transaction machinery:

  4.2 - transaction.on_commit ordering with outer atomic blocks
  4.6 - Broker message loss (on_commit fires but dispatch fails)
  4.7 - Database connection loss during phase 2

These tests validate that the framework behaves correctly when the
infrastructure layer (DB transactions, Celery broker) fails.
"""
import threading
import unittest
from unittest.mock import patch, MagicMock, call

from django.conf import settings
from django.db import transaction, connection, connections
from django.core.cache import cache
from django.test import override_settings, tag

from django_logic import Transition, Process
from django_logic.state import State, RedisState
from django_logic.exceptions import TransitionNotAllowed

from tests.stability.base import StabilityTestCase
from tests.stability.models import (
    Order, OrderProcess,
    side_effect_one, side_effect_two,
)


@tag('stability')
class TestTransactionOnCommitOrdering(StabilityTestCase):
    """
    4.2 -- When phase 1 is nested inside an outer transaction.atomic(),
    on_commit fires only when the OUTER transaction commits.

    If the outer transaction rolls back, the state change must also
    roll back (no orphan in_progress states in DB).
    """

    def test_state_change_inside_outer_atomic_persists_on_commit(self):
        """
        State changes inside an atomic block are visible only after
        the outer transaction commits.
        """
        order = Order.objects.create(status='draft')

        with transaction.atomic():
            process = OrderProcess(field_name='status', instance=order)
            process.approve()

            order_inside = Order.objects.get(pk=order.pk)
            self.assertEqual(order_inside.status, 'approved')

        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')

    def test_state_change_rolled_back_on_outer_atomic_failure(self):
        """
        If the outer transaction rolls back, the state change must
        also be rolled back. This prevents orphan state changes.
        """
        order = Order.objects.create(status='draft')

        try:
            with transaction.atomic():
                process = OrderProcess(field_name='status', instance=order)
                process.approve()

                inside = Order.objects.get(pk=order.pk)
                self.assertEqual(inside.status, 'approved')

                raise ValueError("Outer transaction failure")
        except ValueError:
            pass

        order.refresh_from_db()
        self.assertEqual(order.status, 'draft')

    def test_lock_state_after_rollback(self):
        """
        After a rollback, the cache-based lock may still exist
        (cache ops are not transactional). Verify this edge case.
        """
        order = Order.objects.create(status='draft')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        try:
            with transaction.atomic():
                process = OrderProcess(field_name='status', instance=order)
                process.approve()
                raise ValueError("rollback")
        except ValueError:
            pass

        order.refresh_from_db()
        self.assertEqual(order.status, 'draft')

        # Default mode: the cache lock was released by complete_transition
        # before the rollback (lock -> side_effects -> set_target -> unlock
        # -> callbacks). The DB state rolls back while the lock is already
        # gone — the next transition attempt works, but there is a
        # documented stale-read window between unlock and commit (#141;
        # see TestDeferUnlockTwoConnections below). Projects that need
        # exclusion to cover the whole uncommitted span opt into
        # DJANGO_LOGIC['DEFER_UNLOCK_UNTIL_COMMIT'] — with the trade-off
        # that a rollback then leaves the lock to expire via its TTL
        # (tests/test_defer_unlock.py pins both behaviors).


@tag('stability')
class TestBrokerMessageLoss(StabilityTestCase):
    """
    4.6 -- on_commit fires but Celery apply_async fails (broker down).

    The transition's phase 1 completed (state changed, message row exists
    in the planned design). The periodic starter must be able to recover.

    For now (pre-BackgroundTransition), we test the lock/state behavior
    when a hypothetical dispatch would fail.
    """

    def test_state_persists_even_if_dispatch_would_fail(self):
        """
        Phase 1 completes: state set to in_progress_state, lock acquired.
        If dispatch fails, the state and lock persist in the system.
        """
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

    def test_recovery_after_simulated_broker_failure(self):
        """
        Simulate: phase 1 set state to in_progress, broker lost the message.
        Recovery: manually re-trigger the transition from in_progress_state.
        """
        order = Order.objects.create(status='fulfilling')

        process = OrderProcess(field_name='status', instance=order)
        available = list(process.get_available_transitions(action_name='fulfill'))

        self.assertTrue(len(available) > 0,
            "The 'fulfill' transition should be available from 'fulfilling' "
            "(because in_progress_state is added to sources)")

        process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')


@tag('stability')
class TestDatabaseConnectionLoss(StabilityTestCase):
    """
    4.7 -- Worker's DB connection drops mid-side-effect.

    When the DB connection is lost:
    - The side effect that uses DB will raise OperationalError
    - fail_transition runs (which also needs DB)
    - If fail_transition also fails, the lock should still be released
      (cache-based, independent of DB)

    The periodic starter should eventually re-dispatch.
    """

    def test_db_error_in_side_effect_triggers_failure_path(self):
        """
        A side effect that encounters a DB error triggers the failure path.
        The failed_state should be set (if the DB is available for that).
        """
        from django.db.utils import OperationalError

        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        def db_failing_se(instance, **kwargs):
            raise OperationalError("connection lost")

        process_cls = type('DBFailProcess', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    failed_state='fulfillment_failed',
                    side_effects=[db_failing_se],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(OperationalError):
            process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfillment_failed')
        self.assert_unlocked(state)

    def test_side_effect_db_error_without_failed_state(self):
        """Without failed_state, the state stays at in_progress after DB error."""
        from django.db.utils import OperationalError

        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        def db_failing_se(instance, **kwargs):
            raise OperationalError("connection lost")

        process_cls = type('DBFailNoFailedState', (OrderProcess,), {
            'transitions': [
                Transition(
                    action_name='fulfill',
                    sources=['approved'],
                    target='fulfilled',
                    in_progress_state='fulfilling',
                    side_effects=[db_failing_se],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(OperationalError):
            process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilling')
        self.assert_unlocked(state)


@tag('stability')
class TestDeferUnlockTwoConnections(StabilityTestCase):
    """
    #141 -- the unlock-before-commit window, reproduced on two real
    database connections.

    T1 runs a synchronous transition inside an outer atomic block and
    holds the transaction open. In default mode T1 has already released
    the cache lock, so T2 (another connection) reads the OLD committed
    state, finds it a valid source, and runs the same transition again --
    both executed side-effects and the final state depends on commit
    ordering. With DEFER_UNLOCK_UNTIL_COMMIT the lock is held until T1's
    commit and T2 is rejected while the window is open.
    """

    def _run_t1_holding_transaction_open(self, order, t1_transitioned, t2_probed):
        def t1():
            try:
                with transaction.atomic():
                    OrderProcess(
                        field_name='status',
                        instance=Order.objects.get(pk=order.pk),
                    ).approve()
                    t1_transitioned.set()
                    # Hold the outer transaction open while T2 probes.
                    if not t2_probed.wait(10):
                        raise RuntimeError('T2 never probed')
            finally:
                connections.close_all()

        thread = threading.Thread(target=t1)
        thread.start()
        return thread

    @unittest.skipUnless(connection.vendor == 'postgresql',
                         'needs two concurrent writer connections')
    def test_default_mode_second_transition_reads_stale_committed_state(self):
        """Default mode: the documented window exists — pin it."""
        order = Order.objects.create(status='draft')
        t1_transitioned, t2_probed = threading.Event(), threading.Event()
        thread = self._run_t1_holding_transaction_open(
            order, t1_transitioned, t2_probed)
        try:
            self.assertTrue(t1_transitioned.wait(10))
            # T1 unlocked on completion, but its 'approved' write is
            # invisible to this connection: the committed state is still
            # 'draft', so the SAME transition validates and runs again.
            OrderProcess(
                field_name='status',
                instance=Order.objects.get(pk=order.pk),
            ).approve()
        finally:
            t2_probed.set()
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive())

        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')

    @unittest.skipUnless(connection.vendor == 'postgresql',
                         'needs two concurrent writer connections')
    def test_defer_mode_excludes_second_transition_until_commit(self):
        order = Order.objects.create(status='draft')
        state = State(order, 'status', process_name='process')
        with override_settings(DJANGO_LOGIC={
            **settings.DJANGO_LOGIC, 'DEFER_UNLOCK_UNTIL_COMMIT': True,
        }):
            t1_transitioned, t2_probed = threading.Event(), threading.Event()
            thread = self._run_t1_holding_transaction_open(
                order, t1_transitioned, t2_probed)
            try:
                self.assertTrue(t1_transitioned.wait(10))
                # T1 still holds the lock: T2 must not execute from the
                # old committed source while T1's write is uncommitted.
                with self.assertRaises(TransitionNotAllowed):
                    OrderProcess(
                        field_name='status',
                        instance=Order.objects.get(pk=order.pk),
                    ).approve()
            finally:
                t2_probed.set()
                thread.join(timeout=15)
            self.assertFalse(thread.is_alive())

        # T1's commit ran the deferred unlock (on_commit, in its thread).
        self.assertFalse(state.is_locked())
        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')

        # And the follow-up transition proceeds normally after commit.
        OrderProcess(field_name='status', instance=order).fulfill()
        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')
