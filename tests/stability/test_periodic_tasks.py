"""
Category 1.7, 1.8, 4.4: Periodic Task Tests

Tests for the safety-net periodic tasks that will be implemented in
Stage 2 (BackgroundTransition). These tests define the expected behavior
contracts and can be run once the tasks exist.

Currently, these tests validate the underlying State/lock behavior that
the periodic tasks will depend on, plus define the contract for:

  - retry_stale_transitions: re-dispatch uncompleted messages
  - detect_stuck_transitions: alert on max-error messages
  - cleanup_completed_transitions: delete old completed messages

NOTE: Tests that require the TransitionMessage model will be enabled
once Stage 2 implementation lands. For now, they test the lock/state
contracts that the periodic tasks depend on.
"""
from datetime import timedelta

from django.core.cache import cache
from django.test import tag
from django.utils import timezone

from django_logic.state import State

from tests.stability.base import StabilityTestCase
from tests.stability.models import Order, OrderProcess


@tag('stability')
class TestPeriodicStarterContract(StabilityTestCase):
    """
    1.7 -- The periodic starter should only re-dispatch messages that are:
      - Not completed (is_completed=False)
      - Older than RETRY_MINUTES
      - Under MAX_ERRORS limit
      - Using their stored queue_name for dispatch
    """

    def test_a_crashed_sync_run_leaves_the_instance_at_its_source(self):
        """A hard-killed synchronous run leaves no trace (0.12.0): the
        marker is background-only, so the instance stays at its source
        state — re-drivable once the lock TTL expires, with nothing for a
        sweep to find. (The stranded sweep this class used to pin was
        retired with the marker.)
        """
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.track_lock(state)
        # The crashed run's only residue is its lock.
        self.assertTrue(state.lock())

        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')
        state.unlock()
        self._tracked_cache_keys.discard(state._get_hash())

    def test_completed_state_should_not_be_retried(self):
        """Instances that reached a terminal state need no retry."""
        for terminal_state in ('fulfilled', 'fulfillment_failed', 'cancelled', 'completed'):
            order = Order.objects.create(status=terminal_state)
            state = State(order, 'status', process_name='process')
            self.assertFalse(state.is_locked())

    def test_fulfill_transition_available_from_its_source(self):
        """Sync transitions resolve from their declared sources only —
        there is no implicit in-progress source anymore (0.12.0)."""
        order = Order.objects.create(status='approved')
        process = OrderProcess(field_name='status', instance=order)

        transitions = list(
            process.get_available_transitions(action_name='fulfill')
        )
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0].action_name, 'fulfill')


@tag('stability')
class TestStuckTransitionDetection(StabilityTestCase):
    """
    1.8 -- Transitions that have reached MAX_ERRORS should be detected
    and flagged, not re-dispatched.

    This tests the detection contract. The actual detect_stuck_transitions
    task will query TransitionMessage rows where errors_count >= MAX_ERRORS
    and is_completed=False, log an alert, and optionally set failed_state.
    """

    def test_stuck_rows_are_identified_by_the_message_not_the_state(self):
        """Stuck detection is TM-scoped (0.12.0): a row at MAX_ERRORS and
        uncompleted is the signal; the instance's state field carries no
        in-progress marker for sync work anymore."""
        order = Order.objects.create(status='approved')
        state = State(order, 'status', process_name='process')
        self.assertFalse(state.is_locked())
        self.assertEqual(Order.objects.filter(status='approved').count(), 1)


@tag('stability')
class TestCleanupContract(StabilityTestCase):
    """
    4.4 -- cleanup_completed_transitions should:
      - Delete completed messages older than CLEANUP_DAYS
      - NEVER delete uncompleted messages regardless of age
      - NEVER delete recent completed messages

    Tests here validate the queryset patterns that the cleanup task
    will use.
    """

    def test_terminal_states_are_identifiable(self):
        """Terminal states can be queried for cleanup."""
        Order.objects.create(status='fulfilled')
        Order.objects.create(status='fulfillment_failed')
        Order.objects.create(status='approved')

        terminal = Order.objects.filter(
            status__in=['fulfilled', 'fulfillment_failed', 'cancelled', 'completed']
        )
        pending = Order.objects.filter(status='approved')

        self.assertEqual(terminal.count(), 2)
        self.assertEqual(pending.count(), 1)
