"""Two rules that stop a transition from overwriting a concurrent state change.

``Transition.change_state`` and ``BackgroundTransition.change_state`` re-read
the persisted state after they take the lock, and refuse when another
transition has already moved the row. On refusal the lock is released, no
side-effect runs, and no ``TransitionMessage`` row is left behind.

A transition with no target follows the same rules: it takes the lock up
front, so a foreign holder refuses it before any side-effect runs.
"""
from django.core.cache import cache
from django.test import TestCase

from django_logic import Process
from django_logic.background import BackgroundTransition, sync_execution
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State
from django_logic.transition import Transition
from tests.models import Invoice

SIDE_EFFECT_CALLS = []


def record_side_effect(instance, **kwargs):
    SIDE_EFFECT_CALLS.append(instance.pk)


def raise_boom(instance, **kwargs):
    raise ValueError('boom')


class ApprovalProcess(Process):
    transitions = [
        BackgroundTransition('approve', sources=['draft'], target='approved',
                             side_effects=[record_side_effect]),
    ]


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

        self.assertEqual(cm.exception.reason, 'source_state')
        self.assertEqual(SIDE_EFFECT_CALLS, [])
        # The row still holds what the concurrent writer set.
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'void')

    def test_background_transition_rejects_stale_state_no_message_row(self):
        # BackgroundTransition re-reads inside its own lock, and it does so
        # before it saves the TransitionMessage row.
        Invoice.objects.filter(pk=self.invoice.pk).update(status='void')

        process = ApprovalProcess(instance=self.invoice, field_name='status')
        with sync_execution():
            with self.assertRaises(TransitionNotAllowed) as cm:
                process.approve()

        self.assertEqual(cm.exception.reason, 'source_state')
        # No uncompleted row may survive the refusal.
        self.assertEqual(TransitionMessage.objects.count(), 0)
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


    def test_failed_state_written_when_unlocked(self):
        # Control: with no lock held the failed_state write proceeds, and the
        # side-effect exception still reaches the caller.
        with self.assertRaises(ValueError):
            self.action.change_state(self.state)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'failed')
