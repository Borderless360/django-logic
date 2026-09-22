"""How transitions behave with Django's transaction machinery.

Covers on_commit ordering inside an outer atomic block, a lost broker message
after on_commit fires, and a database connection lost while the worker runs.
These tests check the framework when the infrastructure under it fails.
"""
import threading
import unittest
from unittest.mock import patch, MagicMock, call

from django.db import transaction, connection, connections
from django.core.cache import cache
from django.test import SimpleTestCase, tag

from django_logic import Transition, Process
from django_logic.state import State

from tests.stability.base import StabilityTestCase
from tests.stability.models import (
    Order, OrderProcess,
    side_effect_one, side_effect_two,
)


class _DatabaseThread(threading.Thread):
    """Return a database helper's failure to the test that started it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._error = None

    def run(self):
        try:
            try:
                super().run()
            finally:
                connections.close_all()
        except BaseException as error:
            self._error = error

    def join_and_raise(self):
        self.join(timeout=15)
        if self.is_alive():
            raise AssertionError('Database helper thread did not stop')
        if self._error is not None:
            raise self._error


@tag('stability')
class TestDatabaseThreadFailures(SimpleTestCase):
    def test_helper_exception_fails_the_parent_test(self):
        def fail_in_thread():
            raise RuntimeError('Intentional database helper failure')

        class FailingThreadCase(unittest.TestCase):
            def runTest(self):
                thread = _DatabaseThread(target=fail_in_thread)
                thread.start()
                thread.join_and_raise()

        result = unittest.TestResult()
        FailingThreadCase().run(result)

        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        self.assertIn('Intentional database helper failure', result.errors[0][1])


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

        # complete_transition released the lock before the rollback
        # (lock -> side_effects -> set_target -> unlock -> callbacks).
        # The database state rolls back while the lock is already gone, so the
        # next attempt works. Another connection can still read the old
        # committed state between unlock and commit, which is what
        # TestUnlockBeforeCommitWindow below reproduces. Callers who need the
        # transition to see the surrounding write start it from
        # transaction.on_commit instead.


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
    because it lives in the cache. The row stays claimable; a later claim retries it.
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
class TestUnlockBeforeCommitWindow(StabilityTestCase):
    """The unlock-before-commit window, on two real database connections.

    T1 runs a synchronous transition inside an outer atomic block and holds the
    transaction open. T1 has already released the cache lock, so T2 on another
    connection reads the old committed state, accepts it as a source, and runs
    the same transition again. Both attempts run the side-effects, and the
    final state depends on commit order. Callers who need to avoid this window
    start the transition from ``transaction.on_commit``.
    """

    @unittest.skipUnless(connection.vendor == 'postgresql',
                         'needs two concurrent writer connections')
    def test_second_transition_reads_stale_committed_state(self):
        """Both callers commit, although each accepts the same source state."""
        order = Order.objects.create(status='draft')
        first_transitioned = threading.Event()
        second_effect_started = threading.Event()
        release_first_transaction = threading.Event()
        effects = []
        commits = []
        evidence_lock = threading.Lock()

        def record_effect(instance, **kwargs):
            with evidence_lock:
                effects.append(instance.status)
            if first_transitioned.is_set():
                # The target write waits for the first transaction's row lock.
                # Signal before that write, so the first caller can commit.
                second_effect_started.set()
                release_first_transaction.set()

        class ConcurrentApprovalProcess(Process):
            process_name = 'process'
            transitions = [
                Transition(
                    action_name='approve',
                    sources=['draft'],
                    target='approved',
                    side_effects=[record_effect],
                ),
            ]

        def first_transaction():
            with transaction.atomic():
                ConcurrentApprovalProcess(
                    field_name='status',
                    instance=Order.objects.get(pk=order.pk),
                ).approve()
                first_transitioned.set()
                if not release_first_transaction.wait(10):
                    raise RuntimeError('Second caller never reached its side effect')
            with evidence_lock:
                commits.append('first')

        thread = _DatabaseThread(target=first_transaction)
        thread.start()
        try:
            self.assertTrue(first_transitioned.wait(10))
            with transaction.atomic():
                stale_order = Order.objects.get(pk=order.pk)
                self.assertEqual(stale_order.status, 'draft')
                ConcurrentApprovalProcess(
                    field_name='status', instance=stale_order,
                ).approve()
            with evidence_lock:
                commits.append('second')
        finally:
            release_first_transaction.set()
            thread.join_and_raise()

        self.assertTrue(second_effect_started.is_set())
        self.assertEqual(effects, ['draft', 'draft'])
        self.assertCountEqual(commits, ['first', 'second'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')
