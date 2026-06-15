"""Issue #98: condition-disambiguated background transitions that share an
``action_name`` across nested processes.

A ``ConversationProcess`` routes per messaging integration via two nested
processes — ``GmailConversationProcess`` and ``DummyConversationProcess`` —
each declaring a background ``send_message_via_integration`` and ``close``,
selected by a condition on the instance (``source_integration``). Generic
callers just invoke ``conversation.process.send_message_via_integration(...)``.

Before the fix, declaring the class raised ``ImproperlyConfigured`` because
``_validate_unique_background_action_names`` forbade any two background
transitions sharing an ``action_name`` across the nested tree, and phase-2
restore (``runner._find_transition``) keyed on ``action_name`` alone.

The fix: phase 1 records the owning (nested) process class on the
``TransitionMessage`` (``owning_process_class``); phase 2 uses it to restore
the EXACT background transition without re-evaluating the condition. The
validator now only forbids genuine ambiguity (two background transitions
sharing a name within a single process class) and sync/background name
collisions.
"""
from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import _find_transition
from django_logic.exceptions import TransitionNotAllowed
from tests.background.models import (
    Conversation,
    ConversationProcess,
)


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}

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

        tm = TransitionMessage.objects.get(
            transition_name='send_message_via_integration'
        )
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.errors_count, 0)
        # Bound parent recorded as process_name; the OWNER is the nested class.
        self.assertEqual(tm.process_name, 'process')
        self.assertEqual(tm.owning_process_class, _GMAIL)

    def test_dummy_routes_to_dummy_transition(self):
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        conv.process.send_message_via_integration()

        conv.refresh_from_db()
        self.assertIn('dummy_send,', conv.se_log)
        self.assertNotIn('gmail_send,', conv.se_log)

        tm = TransitionMessage.objects.get(
            transition_name='send_message_via_integration'
        )
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.owning_process_class, _DUMMY)

    def test_close_is_disambiguated_too(self):
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        conv.process.close()

        conv.refresh_from_db()
        self.assertEqual(conv.status, 'closed')
        tm = TransitionMessage.objects.get(transition_name='close')
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.owning_process_class, _DUMMY)

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

    def test_phase1_ambiguity_raises_before_any_state_write_or_row(self):
        # The relaxed validator allows a shared background action_name across
        # distinct nested classes even when the conditions are NOT mutually
        # exclusive. Such a misconfiguration is caught at phase 1: resolution
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
        # The owner's distinct in_progress_state is written in phase 1; it is
        # part of why the two transitions stay distinguishable.
        gmail = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        tm = TransitionMessage.objects.create(
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
        found = _find_transition(process, tm)
        self.assertEqual(found.in_progress_state, 'gmail_sending')


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class FindTransitionDisambiguationTests(TestCase):
    """Focused tests of ``_find_transition`` — the phase-2 selection logic —
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
        # condition would pass), restoring a TM whose recorded owner is the
        # Dummy process resolves to the DUMMY transition. Phase 2 trusts the
        # transition phase 1 chose, not a re-evaluation that could disagree.
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        process = conv.process
        found = _find_transition(process, self._make_tm(conv, owner=_DUMMY))
        self.assertEqual(found.in_progress_state, 'dummy_sending')

    def test_blank_owner_falls_back_to_first_match(self):
        # Backward compatibility: a legacy row (no recorded owner) resolves by
        # first-match while descending nested_processes — the Gmail process is
        # listed first.
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        process = conv.process
        tm = self._make_tm(conv, owner='')
        found = _find_transition(process, tm)
        self.assertEqual(found.in_progress_state, 'gmail_sending')

    def test_unknown_owner_falls_back_to_first_match(self):
        # An owner recorded but no longer present in the tree (renamed/removed
        # between deploys) degrades to first-match rather than failing to
        # restore.
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        process = conv.process
        tm = self._make_tm(conv, owner='tests.background.models.GoneProcess')
        found = _find_transition(process, tm)
        self.assertEqual(found.in_progress_state, 'gmail_sending')
