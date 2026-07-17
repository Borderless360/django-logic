"""Issue #98 follow-up: background -> background ``next_transition`` chaining.

The ``owning_process_class`` overwrite in ``Process._get_transition_method``
(process.py) exists specifically for the chained-``next_transition`` case:
a follow-up transition forwards the predecessor's kwargs, and the owner
must be re-resolved to the follow-up's own declaring process — not
inherited. Until now this path had **no test**: the only ``next_transition``
fixture was sync -> sync (``WidgetChainProcess``).

These scenarios drive the real object through the whole chain via the
``Process`` entrypoint (no mocks, no ``change_state`` patching) and assert
on the observable transformation — the full state trace, the side-effects
that ran, and the per-transition ``owning_process_class`` recorded on each
``TransitionMessage``.

Two cases:

1. A flat ``WidgetBgChainProcess``: ``bg_fulfil`` -> ``bg_export``. The
   follow-up TM must record ``WidgetBgChainProcess`` (not the
   predecessor's), and the object must pass through every intermediate
   state.

2. A nested condition-disambiguated ``ChainConversationProcess``: a
   per-integration nested bg ``send`` chains into a nested bg ``report``.
   The follow-up ``report`` TM must record the NESTED owning class
   (``GmailChainProcess`` / ``DummyChainProcess``), not the bound parent
   and not the predecessor — the riskiest owner-overwrite case.
"""
from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.testing import JourneyStep, ProcessScenario
from tests.background.models import (
    ChainConversationProcess,
    Conversation,
    Widget,
    WidgetBgChainProcess,
)


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}

_BG_CHAIN = 'tests.background.models.WidgetBgChainProcess'
_GMAIL_CHAIN = 'tests.background.models.GmailChainProcess'
_DUMMY_CHAIN = 'tests.background.models.DummyChainProcess'


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class BgToBgChainScenario(ProcessScenario):
    """A background transition whose ``next_transition`` is another
    background transition, driven end-to-end through the entrypoint."""

    process_class = WidgetBgChainProcess
    model = Widget
    state_field = 'status'
    process_name = 'bg_chain'

    def test_chains_and_records_each_owner(self):
        widget = self.create_instance(status='draft')
        self.assert_available(widget, ['bg_fulfil'])

        self.background_transition(widget, 'bg_fulfil')

        # The object passed through EVERY intermediate state — the in_progress
        # and target of each leg of the chain. This is the observable journey,
        # not just the final state.
        self.assert_state_trace(
            ['chain_fulfilling', 'fulfilled', 'chain_exporting', 'exported']
        )
        self.assert_state(widget, 'exported')

        # Both legs' side-effects ran, in order, inside the one drive.
        self.assert_side_effects_ran(['se_bg_fulfil_se', 'se_bg_export_se'])
        self.assert_callbacks_ran(['cb_bg_export_cb'])

        # Two separate durable rows were created, one per background
        # transition — and each records its OWN owner, not the predecessor's.
        self.assert_related_count(TransitionMessage.objects.all(), 2)
        tms = list(TransitionMessage.objects.order_by('id'))
        self.assertEqual([t.transition_name for t in tms], ['bg_fulfil', 'bg_export'])
        self.assertTrue(all(t.is_completed for t in tms))
        self.assert_transition_owner(
            widget, _BG_CHAIN, transition_name='bg_fulfil'
        )
        self.assert_transition_owner(
            widget, _BG_CHAIN, transition_name='bg_export'
        )

    def test_journey_pins_the_whole_transformation(self):
        # The journey assertion locks the end-to-end observable behaviour
        # in one statement: one drive, draft -> exported, both side-effects,
        # the export callback, no failure.
        widget = self.create_instance(status='draft')
        self.background_transition(widget, 'bg_fulfil')
        self.assert_journey([
            JourneyStep(
                action='bg_fulfil',
                before='draft',
                after='exported',
                side_effects=['se_bg_fulfil_se', 'se_bg_export_se'],
                callbacks=['cb_bg_export_cb'],
                failed=False,
            ),
        ])

    def test_failure_of_first_leg_does_not_chain(self):
        # A failure of the first leg stops the chain: the follow-up never
        # runs and no follow-up TransitionMessage is created. The instance
        # stays in the first leg's in_progress state, pending retry.
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'bg_fulfil', fail_side_effect='se_bg_fulfil_se',
            fail_with=ValueError('fulfil broke'),
        )
        self.assert_state(widget, 'chain_fulfilling')
        # Only the first leg's side-effect was attempted (and injected to
        # fail); the export side-effect never ran.
        self.assert_side_effects_not_ran(['se_bg_export_se'])
        # Only one TM exists — the failed first leg, uncompleted for retry.
        self.assertEqual(TransitionMessage.objects.count(), 1)
        self.assertFalse(TransitionMessage.objects.get().is_completed)
        self.assert_transition_owner(widget, _BG_CHAIN, transition_name='bg_fulfil')

    def test_terminal_failure_of_first_leg_does_not_chain(self):
        # When the first leg exhausts MAX_ERRORS it terminalizes into its
        # failed_state; the follow-up still never runs.
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'bg_fulfil', fail_side_effect='se_bg_fulfil_se',
            fail_with=ValueError('persistent'),
        )
        # Drive retries until terminal (MAX_ERRORS total attempts).
        for _ in range(2):  # MAX_ERRORS=3 -> initial + 2 retries
            self.retry_transition(
                widget, fail_side_effect='se_bg_fulfil_se',
                fail_with=ValueError('persistent'),
            )
        self.assert_state(widget, 'chain_fulfil_failed')
        self.assert_side_effects_not_ran(['se_bg_export_se'])
        self.assertEqual(TransitionMessage.objects.count(), 1)
        self.assertTrue(TransitionMessage.objects.get().is_completed)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class NestedDisambiguatedBgChainScenario(ProcessScenario):
    """A nested, condition-disambiguated background chain. The follow-up
    ``report`` must record the NESTED owning class, not the bound parent
    and not the predecessor — the riskiest owner-overwrite case for #98."""

    process_class = ChainConversationProcess
    model = Conversation
    state_field = 'status'
    process_name = 'chain_conv'

    def test_gmail_chain_records_nested_owner_on_each_leg(self):
        conv = self.create_instance(status='open', source_integration='gmail')
        self.background_transition(conv, 'send')

        # open -> gmail_chain_sending -> open -> gmail_chain_reporting -> reported
        self.assert_state_trace(
            ['gmail_chain_sending', 'open', 'gmail_chain_reporting', 'reported']
        )
        self.assert_state(conv, 'reported')
        self.assert_side_effects_ran(['chain_gmail_send', 'chain_gmail_report'])
        self.assertNotIn('dummy_', conv.se_log)

        # Each leg's TM records the NESTED Gmail class as owner — not the
        # bound parent ChainConversationProcess, and the follow-up does NOT
        # inherit the predecessor's owner.
        self.assert_transition_owner(conv, _GMAIL_CHAIN, transition_name='send')
        self.assert_transition_owner(conv, _GMAIL_CHAIN, transition_name='report')

    def test_dummy_chain_records_nested_owner_on_each_leg(self):
        conv = self.create_instance(status='open', source_integration='dummy')
        self.background_transition(conv, 'send')

        self.assert_state_trace(
            ['dummy_chain_sending', 'open', 'dummy_chain_reporting', 'reported']
        )
        self.assert_state(conv, 'reported')
        self.assert_side_effects_ran(['chain_dummy_send', 'chain_dummy_report'])
        self.assertNotIn('gmail_', conv.se_log)

        self.assert_transition_owner(conv, _DUMMY_CHAIN, transition_name='send')
        self.assert_transition_owner(conv, _DUMMY_CHAIN, transition_name='report')

    def test_two_conversations_chain_independently(self):
        gmail = self.create_instance(status='open', source_integration='gmail')
        dummy = self.create_instance(status='open', source_integration='dummy')
        self.background_transition(gmail, 'send')
        self.background_transition(dummy, 'send')

        gmail.refresh_from_db()
        dummy.refresh_from_db()
        self.assertEqual(gmail.status, 'reported')
        self.assertEqual(dummy.status, 'reported')
        self.assertIn('gmail_report,', gmail.se_log)
        self.assertNotIn('dummy_', gmail.se_log)
        self.assertIn('dummy_report,', dummy.se_log)
        self.assertNotIn('gmail_', dummy.se_log)


_STRICT_SYNC_SETTINGS = {**_SYNC_SETTINGS, 'STRICT_KWARGS_SERIALIZATION': True}

_CHAIN_SEEN: dict = {}


def _record_chain_kwargs(instance, **kwargs):
    _CHAIN_SEEN.clear()
    _CHAIN_SEEN.update(kwargs)


@override_settings(DJANGO_LOGIC=_STRICT_SYNC_SETTINGS)
class SyncToBackgroundRequestChainTests(TestCase):
    """#129: a sync transition's next_transition into a BACKGROUND follow-up
    must not forward ``request`` — under STRICT_KWARGS_SERIALIZATION the
    follow-up's phase-1 failure is swallowed by NextTransition, silently
    killing the chain. Sync follow-ups keep receiving request."""

    @classmethod
    def setUpClass(cls):
        from django_logic import Process, Transition
        from django_logic.background import BackgroundTransition
        from django_logic.process import ProcessManager

        super().setUpClass()

        class RequestChainProcess(Process):
            process_name = 'request_chain_process'
            transitions = [
                Transition('kick', sources=['draft'], target='kicked',
                           next_transition='bg_finish'),
                BackgroundTransition('bg_finish', sources=['kicked'], target='done',
                                     side_effects=[_record_chain_kwargs]),
                Transition('kick_sync', sources=['draft'], target='kicked',
                           next_transition='sync_finish'),
                Transition('sync_finish', sources=['kicked'], target='done',
                           side_effects=[_record_chain_kwargs]),
            ]

        cls.process_class = RequestChainProcess
        ProcessManager.bind_model_process(Widget, RequestChainProcess,
                                          state_field='status')

    @classmethod
    def tearDownClass(cls):
        from django_logic.process import ProcessManager

        if 'request_chain_process' in vars(Widget):
            delattr(Widget, 'request_chain_process')
        ProcessManager.bindings = [
            b for b in ProcessManager.bindings if b.process_class is not cls.process_class]
        super().tearDownClass()

    def setUp(self):
        _CHAIN_SEEN.clear()
        self.widget = Widget.objects.create(status='draft')

    def test_background_follow_up_runs_despite_request_under_strict(self):
        self.widget.request_chain_process.kick(request=object())
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'done')
        self.assertNotIn('request', _CHAIN_SEEN)

    def test_sync_follow_up_still_receives_request(self):
        sentinel = object()
        self.widget.request_chain_process.kick_sync(request=sentinel)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'done')
        self.assertIs(_CHAIN_SEEN.get('request'), sentinel)
