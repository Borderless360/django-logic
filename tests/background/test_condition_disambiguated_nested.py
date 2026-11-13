"""Issue #98: condition-disambiguated background transitions that share an
``action_name`` across nested processes.

A ``ConversationProcess`` routes per messaging integration via two nested
processes — ``GmailConversationProcess`` and ``DummyConversationProcess`` —
each declaring a background ``send_message_via_integration`` and ``close``,
selected by a condition on the instance (``source_integration``). Generic
callers just invoke ``conversation.process.send_message_via_integration(...)``.

Before the fix, declaring the class raised ``ImproperlyConfigured`` because
``_validate_unique_background_action_names`` forbade any two background
transitions sharing an ``action_name`` across the nested tree, and execute
restore (``runner._find_transition``) keyed on ``action_name`` alone.

The fix: enqueue records the owning (nested) process class on the
``TransitionMessage`` (``owning_process_class``); execute uses it to restore
the EXACT background transition without re-evaluating the condition. The
validator now only forbids genuine ambiguity (two background transitions
sharing a name within a single process class) and sync/background name
collisions.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import (
    _find_transition,
    _RestoreError,
    run_background_transition,
)
from django_logic.background.safety_nets import detect_stuck_transitions
from django_logic.exceptions import TransitionNotAllowed
from tests.background.models import (
    Conversation,
    ConversationProcess,
    Widget,
)
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)

_GMAIL = 'tests.background.models.GmailConversationProcess'
_DUMMY = 'tests.background.models.DummyConversationProcess'


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class ConditionDisambiguatedNestedBackgroundTests(TestCase):
    def test_class_creation_does_not_raise(self):
        # The whole point: a shared background action_name across nested
        # processes is now a legal configuration.
        self.assertEqual(
            [p.__name__ for p in ConversationProcess.nested_processes],
            ['GmailConversationProcess', 'DummyConversationProcess'],
        )

    def test_gmail_routes_to_gmail_transition(self):
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        tr_id = conv.process.send_message_via_integration()
        self.assertIsNotNone(tr_id)

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'open')          # open -> open
        self.assertIn('gmail_send,', conv.se_log)
        self.assertNotIn('dummy_send,', conv.se_log)
        self.assertIn('cb,', conv.cb_log)

        transition_message = TransitionMessage.objects.get(
            transition_name='send_message_via_integration'
        )
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 0)
        # Bound parent recorded as process_name; the OWNER is the nested class.
        self.assertEqual(transition_message.process_name, 'process')
        self.assertEqual(transition_message.owning_process_class, _GMAIL)

    def test_dummy_routes_to_dummy_transition(self):
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        conv.process.send_message_via_integration()

        conv.refresh_from_db()
        self.assertIn('dummy_send,', conv.se_log)
        self.assertNotIn('gmail_send,', conv.se_log)

        transition_message = TransitionMessage.objects.get(
            transition_name='send_message_via_integration'
        )
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.owning_process_class, _DUMMY)

    def test_close_is_disambiguated_too(self):
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        conv.process.close()

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'closed')
        transition_message = TransitionMessage.objects.get(transition_name='close')
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.owning_process_class, _DUMMY)

    def test_two_conversations_route_independently(self):
        gmail = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        dummy = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        gmail.process.send_message_via_integration()
        dummy.process.send_message_via_integration()

        gmail.refresh_from_db()
        dummy.refresh_from_db()
        self.assertIn('gmail_send,', gmail.se_log)
        self.assertNotIn('dummy_send,', gmail.se_log)
        self.assertIn('dummy_send,', dummy.se_log)
        self.assertNotIn('gmail_send,', dummy.se_log)

    def test_enqueue_ambiguity_raises_before_any_state_write_or_row(self):
        # The relaxed validator allows a shared background action_name across
        # distinct nested classes even when the conditions are NOT mutually
        # exclusive. Such a misconfiguration is caught at enqueue: resolution
        # finds two available transitions and raises — and it must do so before
        # any in_progress_state write or TransitionMessage row exists (so no
        # instance is stranded and no orphan row blocks future work).
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        with self.assertRaises(TransitionNotAllowed):
            conv.ambiguous_process.ambiguous_send()

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'open')  # no in_progress_state written
        self.assertEqual(conv.se_log, '')      # no side-effect ran
        self.assertEqual(
            TransitionMessage.objects.filter(instance_id=str(conv.pk)).count(),
            0,
        )

    def test_in_progress_state_is_owner_specific(self):
        # The owner's distinct in_progress_state is written in enqueue; it is
        # part of why the two transitions stay distinguishable.
        gmail = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='conversation',
            instance_id=str(gmail.pk),
            process_name='process',
            transition_name='send_message_via_integration',
            owning_process_class=_GMAIL,
            queue_name='django_logic.critical',
            # Synthetic lookup row: mark completed so several can coexist
            # without tripping the one-uncompleted-per-process constraint.
            is_completed=True,
        )
        process = gmail.process
        found = _find_transition(process, transition_message)
        self.assertEqual(found.in_progress_state, 'gmail_sending')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class FindTransitionDisambiguationTests(TestCase):
    """Focused tests of ``_find_transition`` — the transition lookup the worker uses —
    proving it keys on the recorded owner, not on a condition re-evaluation."""

    def _make_tm(self, conv, *, owner):
        # Synthetic rows exercising the lookup only — mark completed so several
        # can coexist for one instance+process (the partial unique constraint
        # only guards uncompleted rows).
        return TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='conversation',
            instance_id=str(conv.pk),
            process_name='process',
            transition_name='send_message_via_integration',
            owning_process_class=owner,
            queue_name='django_logic.critical',
            is_completed=True,
        )

    def test_owner_path_selects_the_recorded_transition(self):
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        process = conv.process

        gmail_tr = _find_transition(process, self._make_tm(conv, owner=_GMAIL))
        dummy_tr = _find_transition(process, self._make_tm(conv, owner=_DUMMY))

        self.assertEqual(gmail_tr.in_progress_state, 'gmail_sending')
        self.assertEqual(dummy_tr.in_progress_state, 'dummy_sending')
        self.assertIsNot(gmail_tr, dummy_tr)

    def test_owner_path_ignores_the_condition(self):
        # The clincher for Approach 1 over Approach 2: even though the
        # instance's source_integration says 'gmail' (so only the gmail
        # condition would pass), restoring a row whose recorded owner is the
        # Dummy process resolves to the DUMMY transition. Execute trusts the
        # transition enqueue chose, not a re-evaluation that could disagree.
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        process = conv.process
        found = _find_transition(process, self._make_tm(conv, owner=_DUMMY))
        self.assertEqual(found.in_progress_state, 'dummy_sending')

    def test_blank_owner_ambiguous_name_refuses_to_guess(self):
        # Missing identity must never select an arbitrary sibling or run a provider.
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        process = conv.process
        transition_message = self._make_tm(conv, owner='')
        with self.assertRaises(_RestoreError) as ctx:
            _find_transition(process, transition_message)
        self.assertIn('refusing to guess', str(ctx.exception))

    def test_unknown_owner_ambiguous_name_refuses_to_guess(self):
        # An owner recorded but no longer present in the tree (renamed/removed
        # between deploys) for an AMBIGUOUS name likewise refuses to guess rather
        # than first-matching the wrong sibling.
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        process = conv.process
        transition_message = self._make_tm(conv, owner='tests.background.models.GoneProcess')
        with self.assertRaises(_RestoreError):
            _find_transition(process, transition_message)

    def test_unique_name_requires_the_exact_owner(self):
        widget = Widget.objects.create(status='draft')
        process = widget.parent_process
        for owner in ('', 'tests.background.models.GoneProcess'):
            with self.subTest(owner=owner):
                message = TransitionMessage(
                    transition_name='nested_fulfil', owning_process_class=owner,
                )
                with self.assertRaisesMessage(_RestoreError, 'refusing to guess'):
                    _find_transition(process, message)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class PullCrossProcessRestoreTests(TestCase):
    """Issue #98 in the REAL production execution model (web process → worker process).

    The sync-mode tests run enqueue and execute in one process; the durable
    discriminator only matters when they are SEPARATE processes. Here enqueue
    runs in pull mode, so it records the declaring nested process on the row
    and stops — execute does NOT run inline. We then invoke run_background_transition(tm_pk) exactly as a worker
    draining the queue would: a fresh restore from the committed row alone, with
    none of the Python state enqueue had. This proves the owner survives the process boundary
    purely via the DB column.
    """

    def _enqueue_and_capture_row(self, conv):
        # Pull mode: enqueue commits the row and stops — execute runs on a
        # worker process. The captured pk is what a worker would claim.
        with override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull')):
            with self.captureOnCommitCallbacks(execute=False):
                tr_id = conv.process.send_message_via_integration()
        self.assertIsNotNone(tr_id)
        from django_logic.background.models import TransitionMessage
        return TransitionMessage.objects.latest('id').pk

    def test_gmail_round_trip_across_the_worker_boundary(self):
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        tm_pk = self._enqueue_and_capture_row(conv)

        # Enqueue only: the worker has not run. State is in_progress, the row is
        # uncompleted, the owner is recorded, and NO side-effect has fired.
        conv.refresh_from_db()
        self.assertEqual(conv.status, 'gmail_sending')
        transition_message = TransitionMessage.objects.get(pk=tm_pk)
        self.assertFalse(transition_message.is_completed)
        self.assertEqual(transition_message.owning_process_class, _GMAIL)
        self.assertEqual(conv.se_log, '')

        # The worker drains the queue — fresh restore from the committed row.
        run_background_transition(tm_pk)

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'open')           # open -> open completed
        self.assertIn('gmail_send,', conv.se_log)       # the GMAIL side-effect
        self.assertNotIn('dummy_send,', conv.se_log)
        self.assertIn('cb,', conv.cb_log)
        self.assertTrue(TransitionMessage.objects.get(pk=tm_pk).is_completed)

    def test_dummy_round_trip_across_the_worker_boundary(self):
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        tm_pk = self._enqueue_and_capture_row(conv)

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'dummy_sending')
        self.assertEqual(
            TransitionMessage.objects.get(pk=tm_pk).owning_process_class, _DUMMY
        )

        run_background_transition(tm_pk)

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'open')
        self.assertIn('dummy_send,', conv.se_log)       # the DUMMY side-effect
        self.assertNotIn('gmail_send,', conv.se_log)
        self.assertTrue(TransitionMessage.objects.get(pk=tm_pk).is_completed)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class SafetyNetDisambiguationTests(TestCase):
    """The watchdog / detect_stuck safety net restores via the recorded owner
    too (it goes through the same _restore → _find_transition), so a stuck
    condition-disambiguated nested row is finalized into the CORRECT sibling's
    ``failed_state``. Under the old first-match restore a stuck Dummy row would
    have been finalized with Gmail's failed_state — Gmail is first in
    ``nested_processes`` — silently writing the wrong terminal state.
    """

    def _make_stuck(self, conv, *, owner, in_progress):
        conv.status = in_progress  # the in_progress_state enqueue wrote, for the state guard
        conv.save(update_fields=['status'])
        return TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='conversation',
            instance_id=str(conv.pk),
            process_name='process', field_name='status',
            kwargs={'process_class': 'tests.background.models.ConversationProcess'},
            transition_name='send_message_via_integration',
            owning_process_class=owner,
            queue_name='django_logic.critical',
            errors_count=3,  # == MAX_ERRORS → terminal
        )

    def test_stuck_gmail_row_finalizes_to_gmail_failed_state(self):
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        self._make_stuck(conv, owner=_GMAIL, in_progress='gmail_sending')

        self.assertEqual(detect_stuck_transitions(), 1)

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'gmail_send_failed')
        self.assertTrue(
            TransitionMessage.objects.get(instance_id=str(conv.pk)).is_completed
        )

    def test_stuck_dummy_row_finalizes_to_dummy_failed_state(self):
        # The discriminating case: owner = Dummy, but Gmail is first in the
        # tree. The safety net must write dummy_send_failed, not gmail_send_failed.
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        self._make_stuck(conv, owner=_DUMMY, in_progress='dummy_sending')

        self.assertEqual(detect_stuck_transitions(), 1)

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'dummy_send_failed')
        self.assertNotEqual(conv.status, 'gmail_send_failed')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class OwnerlessAmbiguousContainmentTests(TestCase):
    """Missing owner metadata cannot select one of two matching actions.

    Both actions share their source state, so state alone cannot identify which
    provider should run. The worker records the restoration failure instead.
    """

    def test_ownerless_ambiguous_background_action_runs_nothing(self):
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='conversation',
            instance_id=str(conv.pk),
            process_name='shared_action_process',  # bound parent of the siblings
            transition_name='shared_sync',
            owning_process_class='',                # owner lost
            queue_name='django_logic.fast',
        )

        # Worker picks it up. No exception escapes (unrestorable → contained).
        run_background_transition(transition_message.pk)

        conv.refresh_from_db()
        self.assertEqual(conv.se_log, '')  # neither act_a nor act_b ran
        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)   # finalized so retries stop

    def test_ownerless_unique_background_action_runs_nothing(self):
        widget = Widget.objects.create(status='fulfilled')
        message = TransitionMessage.objects.create(
            app_label='bg_tests', model_name='widget', instance_id=str(widget.pk),
            process_name='process', field_name='status', transition_name='sync_inventory',
            owning_process_class='', queue_name='django_logic.fast',
            kwargs={'process_class': 'tests.background.models.WidgetProcess'},
        )
        run_background_transition(message.pk)
        widget.refresh_from_db()
        self.assertEqual(widget.se_log, '')
        message.refresh_from_db()
        self.assertTrue(message.ended_in_failure)
        self.assertIn('requires owning_process_class', message.last_error_message)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class SyncBackgroundSharedNameTests(TestCase):
    """A synchronous and a background transition may now share an action_name
    in one process, routed by a condition. Enqueue resolves the call by
    conditions; execute (is_background filter) only ever restores the background
    one, so the synchronous namesake never interferes."""

    def test_condition_routes_to_synchronous_variant(self):
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        conv.mixed_process.archive()

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'archived_sync')   # ran inline
        self.assertIn('sync_archive,', conv.se_log)
        self.assertNotIn('bg_archive,', conv.se_log)
        # Synchronous path creates no durable row.
        self.assertFalse(
            TransitionMessage.objects.filter(instance_id=str(conv.pk)).exists()
        )

    def test_condition_routes_to_background_variant(self):
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        conv.mixed_process.archive()

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'archived_bg')     # durable, completed
        self.assertIn('bg_archive,', conv.se_log)
        self.assertNotIn('sync_archive,', conv.se_log)
        transition_message = TransitionMessage.objects.get(instance_id=str(conv.pk))
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.transition_name, 'archive')
        self.assertEqual(
            transition_message.owning_process_class, 'tests.background.models.MixedSyncBgProcess'
        )
