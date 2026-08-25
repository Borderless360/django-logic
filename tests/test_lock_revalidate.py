"""Two rules that stop a transition from overwriting a concurrent state change.

``Transition.change_state`` and ``BackgroundTransition.change_state`` re-read
the persisted state after they take the lock, and refuse when another
transition has already moved the row. On refusal the lock is released, no
side-effect runs, and no ``TransitionMessage`` row is left behind.

A transition with no target follows the same rules: it takes the lock up
front, so a foreign holder refuses it before any side-effect runs.
"""
from django.core.cache import cache
from django.test import TestCase, override_settings

from django_logic.background import BackgroundTransition, sync_execution
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State
from django_logic.transition import Transition
from tests.models import Invoice
from tests import dl_settings

SIDE_EFFECT_CALLS = []


def record_side_effect(instance, **kwargs):
    SIDE_EFFECT_CALLS.append(instance.pk)


def raise_boom(instance, **kwargs):
    raise ValueError('boom')


class TransitionLockRevalidationTests(TestCase):
    """change_state re-reads the persisted state after it takes the lock."""

    def setUp(self):
        SIDE_EFFECT_CALLS.clear()
        cache.clear()
        self.invoice = Invoice.objects.create(status='draft')
        self.state = State(self.invoice, 'status', 'process')

    def tearDown(self):
        cache.clear()

    def test_sync_transition_rejects_stale_in_memory_state(self):
        # Another writer moves the row to 'void'. The in-memory instance still
        # says 'draft', so the re-read under the lock must refuse.
        Invoice.objects.filter(pk=self.invoice.pk).update(status='void')
        self.assertEqual(self.invoice.status, 'draft')  # in-memory is stale

        transition = Transition(
            'approve', sources=['draft'], target='approved',
            side_effects=[record_side_effect],
        )
        with self.assertRaises(TransitionNotAllowed) as cm:
            transition.change_state(self.state)

        self.assertIn('persisted state', str(cm.exception))
        self.assertFalse(self.state.is_locked())
        self.assertEqual(SIDE_EFFECT_CALLS, [])
        # The row still holds what the concurrent writer set.
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'void')

    def test_background_transition_rejects_stale_state_no_message_row(self):
        # BackgroundTransition re-reads inside its own lock, and it does so
        # before it saves the TransitionMessage row.
        Invoice.objects.filter(pk=self.invoice.pk).update(status='void')

        transition = BackgroundTransition(
            'approve', sources=['draft'], target='approved',
            side_effects=[record_side_effect],
        )
        with sync_execution():
            with self.assertRaises(TransitionNotAllowed) as cm:
                transition.change_state(self.state)

        self.assertIn('persisted state', str(cm.exception))
        # No uncompleted row may survive the refusal.
        self.assertEqual(TransitionMessage.objects.count(), 0)
        self.assertFalse(self.state.is_locked())
        self.assertEqual(SIDE_EFFECT_CALLS, [])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'void')

    def test_happy_path_proceeds_when_db_state_matches(self):
        # Control: the persisted state matches, so the transition runs.
        transition = Transition(
            'approve', sources=['draft'], target='approved',
            side_effects=[record_side_effect],
        )
        transition.change_state(self.state)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'approved')
        self.assertEqual(SIDE_EFFECT_CALLS, [self.invoice.pk])
        self.assertFalse(self.state.is_locked())


class NoTargetTransitionLockTests(TestCase):
    """A transition with no target holds the lock like any other."""

    def setUp(self):
        cache.clear()
        self.invoice = Invoice.objects.create(status='draft')
        self.state = State(self.invoice, 'status', 'process')
        self.action = Transition(
            'a', sources=['draft'], failed_state='failed',
            side_effects=[raise_boom],
        )

    def tearDown(self):
        cache.clear()

    def test_a_foreign_lock_refuses_it_before_side_effects_run(self):
        # The lock key derives from the instance and the field only, so a
        # second State object for the same row takes the same lock.
        foreign_state = State(
            Invoice.objects.get(pk=self.invoice.pk), 'status', 'process'
        )
        self.assertTrue(foreign_state.lock())

        with self.assertRaises(TransitionNotAllowed):
            self.action.change_state(self.state)

        # Nothing ran and nothing was written: the raise came from the
        # lock, not from the side-effect.
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')
        # The foreign holder keeps its lock.
        self.assertTrue(foreign_state.is_locked())

    def test_failed_state_written_when_unlocked(self):
        # Control: with no lock held the failed_state write proceeds, and the
        # side-effect exception still reaches the caller.
        with self.assertRaises(ValueError):
            self.action.change_state(self.state)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.state.is_locked())
