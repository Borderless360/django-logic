"""D1 + D3 regression tests (0.4 stability hardening).

D1 — under-the-lock revalidation: ``Transition.change_state`` and
``BackgroundTransition.change_state`` must re-read the *persisted* state
after acquiring the lock and refuse to run when a concurrent transition
has already moved the row (validate-then-lock TOCTOU). On rejection the
lock must be released, no side-effects may run, and (for background)
no ``TransitionMessage`` row may be created.

D3 — ``Action.fail_transition`` must NOT write ``failed_state`` while the
state is locked by another in-flight transition (an Action holds no lock,
so writing would clobber the lock holder's state); when unlocked, the
``failed_state`` write proceeds as before.
"""
from django.core.cache import cache
from django.test import TestCase, override_settings

from django_logic.background import BackgroundTransition, sync_execution
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import TransitionNotAllowed
from django_logic.state import State
from django_logic.transition import Action, Transition
from tests.models import Invoice

_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}

SIDE_EFFECT_CALLS = []


def record_side_effect(instance, **kwargs):
    SIDE_EFFECT_CALLS.append(instance.pk)


def raise_boom(instance, **kwargs):
    raise ValueError('boom')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class TransitionLockRevalidationTests(TestCase):
    """D1 — persisted-state revalidation under the lock."""

    def setUp(self):
        SIDE_EFFECT_CALLS.clear()
        cache.clear()
        self.invoice = Invoice.objects.create(status='draft')
        self.state = State(self.invoice, 'status', 'process')

    def tearDown(self):
        cache.clear()

    def test_sync_transition_rejects_stale_in_memory_state(self):
        # D1: flip the DB row out from under the in-memory instance — the
        # instance attribute still says 'draft' but the persisted state is
        # 'void', so the under-the-lock revalidation must reject.
        Invoice.objects.filter(pk=self.invoice.pk).update(status='void')
        self.assertEqual(self.invoice.status, 'draft')  # in-memory is stale

        transition = Transition(
            'approve', sources=['draft'], target='approved',
            side_effects=[record_side_effect],
        )
        with self.assertRaises(TransitionNotAllowed) as cm:
            transition.change_state(self.state)

        self.assertIn('persisted state', str(cm.exception))
        # The lock acquired by change_state must be released on rejection.
        self.assertFalse(self.state.is_locked())
        # No side-effects may have run.
        self.assertEqual(SIDE_EFFECT_CALLS, [])
        # The DB row is untouched — still what the concurrent writer set.
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'void')

    def test_background_transition_rejects_stale_state_no_message_row(self):
        # D1 (background): same revalidation under BackgroundTransition's
        # critical-section lock — and crucially BEFORE the durable
        # TransitionMessage row is created. queue= omitted (optional in 0.4).
        Invoice.objects.filter(pk=self.invoice.pk).update(status='void')

        transition = BackgroundTransition(
            'approve', sources=['draft'], target='approved',
            side_effects=[record_side_effect],
        )
        with sync_execution():
            with self.assertRaises(TransitionNotAllowed) as cm:
                transition.change_state(self.state)

        self.assertIn('persisted state', str(cm.exception))
        # No durable in-flight marker may exist after the rejection.
        self.assertEqual(TransitionMessage.objects.count(), 0)
        # The lock is released by the finally in change_state.
        self.assertFalse(self.state.is_locked())
        self.assertEqual(SIDE_EFFECT_CALLS, [])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'void')

    def test_happy_path_proceeds_when_db_state_matches(self):
        # D1 control: when the persisted state matches the in-memory state,
        # the revalidation is a no-op and the transition completes normally.
        transition = Transition(
            'approve', sources=['draft'], target='approved',
            side_effects=[record_side_effect],
        )
        transition.change_state(self.state)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'approved')
        self.assertEqual(SIDE_EFFECT_CALLS, [self.invoice.pk])
        self.assertFalse(self.state.is_locked())


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class ActionFailedStateLockGuardTests(TestCase):
    """D3 — Action skips its failed_state write under a foreign lock."""

    def setUp(self):
        cache.clear()
        self.invoice = Invoice.objects.create(status='draft')
        self.state = State(self.invoice, 'status', 'process')
        self.action = Action(
            'a', sources=['draft'], failed_state='failed',
            side_effects=[raise_boom],
        )

    def tearDown(self):
        cache.clear()

    def test_failed_state_write_skipped_while_foreign_lock_held(self):
        # D3: another transition holds the lock for this instance/field
        # (the lock key derives from instance + field only, so a second
        # State object for the same row maps to the same key).
        foreign_state = State(
            Invoice.objects.get(pk=self.invoice.pk), 'status', 'process'
        )
        self.assertTrue(foreign_state.lock())

        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            with self.assertRaises(ValueError):
                self.action.change_state(self.state)

        # The skip is logged at ERROR on the transition logger.
        self.assertTrue(
            any('skipping failed_state' in message for message in logs.output),
            f"expected 'skipping failed_state' error log, got: {logs.output}",
        )
        # failed_state was NOT written — the lock holder's state survives.
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')
        # And the foreign lock is still held (Action must not unlock it).
        self.assertTrue(foreign_state.is_locked())

    def test_failed_state_written_when_unlocked(self):
        # D3 control: with no foreign lock the failed_state write proceeds,
        # and the side-effect exception still propagates.
        with self.assertRaises(ValueError):
            self.action.change_state(self.state)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.state.is_locked())
