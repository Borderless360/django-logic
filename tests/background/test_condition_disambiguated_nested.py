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
from unittest.mock import patch

from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import (
    _find_transition,
    _RestoreError,
    run_background_transition,
)
from django_logic.background.tasks import detect_stuck_transitions
from django_logic.exceptions import TransitionNotAllowed
from tests.background.models import (
    Conversation,
    ConversationProcess,
    Widget,
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

    def test_blank_owner_ambiguous_name_refuses_to_guess(self):
        # An owner-less row (legacy / in flight across the deploy that first
        # shared this background action_name across nested processes) whose name
        # is AMBIGUOUS must NOT be resolved by first-match — that would be a coin
        # flip between condition-disambiguated siblings. Restore refuses and
        # raises _RestoreError so the row is finalized without side-effects.
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        process = conv.process
        tm = self._make_tm(conv, owner='')
        with self.assertRaises(_RestoreError) as ctx:
            _find_transition(process, tm)
        self.assertIn('refusing to guess', str(ctx.exception))

    def test_unknown_owner_ambiguous_name_refuses_to_guess(self):
        # An owner recorded but no longer present in the tree (renamed/removed
        # between deploys) for an AMBIGUOUS name likewise refuses to guess rather
        # than first-matching the wrong sibling.
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        process = conv.process
        tm = self._make_tm(conv, owner='tests.background.models.GoneProcess')
        with self.assertRaises(_RestoreError):
            _find_transition(process, tm)

    def test_blank_owner_unique_name_still_resolves(self):
        # Backward compatibility for the COMMON legacy case: a row with no
        # recorded owner whose action_name is UNIQUE across the tree resolves
        # cleanly by name (this is what every pre-discriminator row relied on).
        # nested_fulfil exists only on NestedBgChildProcess under parent_process.
        widget = Widget.objects.create(status='draft')
        process = widget.parent_process
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(widget.pk),
            process_name='parent_process',
            transition_name='nested_fulfil',
            owning_process_class='',
            queue_name='django_logic.critical',
            is_completed=True,
        )
        found = _find_transition(process, tm)
        self.assertEqual(found.action_name, 'nested_fulfil')
        self.assertEqual(found.in_progress_state, 'nested_fulfilling')


_CELERY_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'celery',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}

_APPLY_ASYNC = (
    'django_logic.background.tasks.run_background_transition_task.apply_async'
)


@override_settings(DJANGO_LOGIC=_CELERY_SETTINGS)
class CeleryCrossProcessRestoreTests(TestCase):
    """Issue #98 in the REAL production execution model (web dyno → worker dyno).

    The sync-mode tests run phase 1 and phase 2 in one process; the durable
    discriminator only matters when they are SEPARATE processes. Here phase 1
    runs in celery mode (apply_async mocked, on_commit fired) so it records the
    owning nested process on the row and enqueues — but phase 2 does NOT run
    inline. We then invoke run_background_transition(tm_pk) exactly as a worker
    draining the queue would: a fresh restore from the committed row alone, with
    no phase-1 Python state. This proves the owner survives the process boundary
    purely via the DB column.
    """

    def _phase_one_capture_tm(self, conv):
        with patch(_APPLY_ASYNC) as mock_async:
            with self.captureOnCommitCallbacks(execute=True):
                tr_id = conv.process.send_message_via_integration()
            self.assertIsNotNone(tr_id)
            mock_async.assert_called_once()
            return mock_async.call_args.kwargs['args'][0]

    def test_gmail_round_trip_across_the_worker_boundary(self):
        conv = Conversation.objects.create(
            status='open', source_integration='gmail'
        )
        tm_pk = self._phase_one_capture_tm(conv)

        # Phase 1 only: the worker has not run. State is in_progress, the row is
        # uncompleted, the owner is recorded, and NO side-effect has fired.
        conv.refresh_from_db()
        self.assertEqual(conv.status, 'gmail_sending')
        tm = TransitionMessage.objects.get(pk=tm_pk)
        self.assertFalse(tm.is_completed)
        self.assertEqual(tm.owning_process_class, _GMAIL)
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
        tm_pk = self._phase_one_capture_tm(conv)

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
        conv.status = in_progress  # phase-1 in_progress_state for the state guard
        conv.save(update_fields=['status'])
        return TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='conversation',
            instance_id=str(conv.pk),
            process_name='process',
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
    """The worst case the deeper review flagged: a row that has LOST its owner
    (blank — legacy/pre-discriminator, or in flight across the deploy that first
    shared the name) for an ambiguous BackgroundAction. Both nested actions
    share sources and have no in_progress_state, so the phase-2 state guard
    CANNOT tell them apart — first-match would silently run the wrong
    integration's external side-effects. Phase 2 must instead refuse and contain
    the row (finalize, no side-effects), never running act_a OR act_b.
    """

    def test_ownerless_ambiguous_background_action_runs_nothing(self):
        conv = Conversation.objects.create(
            status='open', source_integration='dummy'
        )
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='conversation',
            instance_id=str(conv.pk),
            process_name='shared_action_process',  # bound parent of the siblings
            transition_name='shared_sync',
            owning_process_class='',                # owner lost
            queue_name='django_logic.fast',
        )

        # Worker picks it up. No exception escapes (unrestorable → contained).
        run_background_transition(tm.pk)

        conv.refresh_from_db()
        self.assertEqual(conv.se_log, '')  # neither act_a nor act_b ran
        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)   # finalized so retries stop

    def test_ownerless_unique_background_action_still_runs(self):
        # Control: the same owner-less row, but for a UNIQUE action name, still
        # resolves and runs — only AMBIGUOUS owner-less rows are refused.
        widget = Widget.objects.create(status='fulfilled')
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(widget.pk),
            process_name='process',
            transition_name='sync_inventory',   # unique BackgroundAction
            owning_process_class='',
            queue_name='django_logic.fast',
        )
        run_background_transition(tm.pk)
        widget.refresh_from_db()
        self.assertIn('ok,', widget.se_log)
        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
