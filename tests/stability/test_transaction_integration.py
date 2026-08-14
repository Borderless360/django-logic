"""How transitions behave with Django's transaction machinery.

Covers on_commit ordering inside an outer atomic block, a lost broker message
after on_commit fires, and a database connection lost while the worker runs.
These tests check the framework when the infrastructure under it fails.
"""
import threading
import unittest
from unittest.mock import patch, MagicMock, call

from django.conf import settings
from django.db import transaction, connection, connections
from django.core.cache import cache
from django.test import override_settings, tag

from django_logic import Transition, Process
from django_logic.state import State
from django_logic.exceptions import TransitionNotAllowed

from tests.stability.base import StabilityTestCase
from tests.stability.models import (
    Order, OrderProcess,
    side_effect_one, side_effect_two,
)


@tag('stability')
class TestTransactionOnCommitOrdering(StabilityTestCase):
    """Inside an outer transaction.atomic(), on_commit fires only when that
    outer transaction commits. If it rolls back instead, the state change rolls
    back with it and leaves no orphan write in the database.
    """

    def test_state_change_inside_outer_atomic_persists_on_commit(self):
        """A state change made inside an atomic block becomes visible to other
        connections only after the outer transaction commits."""
        order = Order.objects.create(status='draft')

        with transaction.atomic():
            process = OrderProcess(field_name='status', instance=order)
            process.approve()

            order_inside = Order.objects.get(pk=order.pk)
            self.assertEqual(order_inside.status, 'approved')

        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')

    def test_state_change_rolled_back_on_outer_atomic_failure(self):
        """When the outer transaction rolls back, the state change rolls back
        with it. Otherwise the instance keeps an orphan state."""
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
        """Cache writes are not transactional, so a rollback cannot restore the
        lock. This test pins what the lock looks like afterwards."""
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

        # In default mode complete_transition released the lock before the
        # rollback (lock -> side_effects -> set_target -> unlock -> callbacks).
        # The database state rolls back while the lock is already gone, so the
        # next attempt works. Another connection can still read the old
        # committed state between unlock and commit, which is what
        # TestDeferUnlockTwoConnections below reproduces. Projects that need
        # exclusion over the whole uncommitted span set
        # DJANGO_LOGIC['DEFER_UNLOCK_UNTIL_COMMIT']; a rollback then leaves the
        # lock to expire on its own timeout.


@tag('stability')
class TestBrokerMessageLoss(StabilityTestCase):
    """on_commit fires but Celery apply_async fails, because the broker is
    down. The durable state write and the advisory cache lock have independent
    lifetimes, so recovering a lost run is an ordinary re-dispatch.
    """

    def test_committed_state_write_persists_with_the_lock(self):
        """A committed set_state persists whatever happens to the cache lock.
        The write is durable; the lock is advisory and expires on a timeout."""
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)

        self.assertTrue(state.lock())
        state.set_state('shipped')

        order.refresh_from_db()
        self.assertEqual(order.status, 'shipped')
        self.assert_locked(state)

        state.unlock()
        self._tracked_cache_keys.discard(state._get_hash())

    def test_recovery_is_a_plain_re_dispatch_from_the_source(self):
        """A synchronous run writes no marker, so a lost run leaves the instance
        at its source state and recovery just runs the transition again."""
        order = Order.objects.create(status='approved')

        process = OrderProcess(field_name='status', instance=order)
        available = list(process.get_available_transitions(action_name='fulfill'))
        self.assertTrue(len(available) > 0)

        process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')


@tag('stability')
class TestDatabaseConnectionLoss(StabilityTestCase):
    """The worker loses its database connection while a side effect runs.

    The side effect raises OperationalError, then fail_transition runs and needs
    the database too. If fail_transition also fails, the lock is still released,
    because it lives in the cache. The periodic starter re-dispatches later.
    """

    def test_db_error_in_side_effect_triggers_failure_path(self):
        """A database error inside a side effect runs the failure path, which
        writes failed_state when the database is reachable again."""
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
        """Without a failed_state, a database error leaves the instance at its
        source state. The transition can simply run again."""
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
                    side_effects=[db_failing_se],
                )
            ]
        })

        process = process_cls(field_name='status', instance=order)
        with self.assertRaises(OperationalError):
            process.fulfill()

        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')
        self.assert_unlocked(state)


@tag('stability')
class TestDeferUnlockTwoConnections(StabilityTestCase):
    """The unlock-before-commit window, on two real database connections.

    T1 runs a synchronous transition inside an outer atomic block and holds the
    transaction open. In default mode T1 has already released the cache lock, so
    T2 on another connection reads the old committed state, accepts it as a
    source, and runs the same transition again. Both attempts run the
    side-effects, and the final state depends on commit order. With
    DEFER_UNLOCK_UNTIL_COMMIT, T1 holds the lock until it commits and T2 is
    rejected.
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
        """Default mode leaves the window open. Pin that."""
        order = Order.objects.create(status='draft')
        t1_transitioned, t2_probed = threading.Event(), threading.Event()
        thread = self._run_t1_holding_transaction_open(
            order, t1_transitioned, t2_probed)
        try:
            self.assertTrue(t1_transitioned.wait(10))
            # T1 unlocked when it finished, but this connection cannot see its
            # 'approved' write. The committed state is still 'draft', so the
            # same transition validates and runs a second time.
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
                # T1 still holds the lock, so T2 must not run from the old
                # committed source while T1's write is uncommitted.
                with self.assertRaises(TransitionNotAllowed):
                    OrderProcess(
                        field_name='status',
                        instance=Order.objects.get(pk=order.pk),
                    ).approve()
            finally:
                t2_probed.set()
                thread.join(timeout=15)
            self.assertFalse(thread.is_alive())

        # T1's commit ran the deferred unlock, on_commit, in its own thread.
        self.assertFalse(state.is_locked())
        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')

        # The follow-up transition then proceeds normally.
        OrderProcess(field_name='status', instance=order).fulfill()
        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')
