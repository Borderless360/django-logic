"""One background transition chained to another with ``next_transition``.

A follow-up forwards the kwargs of the transition before it, so
``Process._get_transition_method`` must resolve the owner again from the
follow-up's own declaring process. An inherited owner would make the worker
restore the wrong transition from the row.

Both scenarios drive the real object through the whole chain from the process
entrypoint, with no mocks. They assert on what is observable: the full state
trace, the side-effects that ran, and the ``owning_process_class`` recorded on
each ``TransitionMessage``.

The flat case chains ``bg_fulfil`` into ``bg_export``. The nested case picks a
``send`` per integration by condition and chains it into a nested ``report``, so
the follow-up row must record the nested class and not the bound parent.
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
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)

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

        # The object passed through every intermediate state: the in-progress
        # state and the target of each step.
        self.assert_state_trace(
            ['chain_fulfilling', 'fulfilled', 'chain_exporting', 'exported']
        )
        self.assert_state(widget, 'exported')

        # Both steps' side-effects ran, in order, in the one call.
        self.assert_side_effects_ran(['se_bg_fulfil_se', 'se_bg_export_se'])
        self.assert_callbacks_ran(['cb_bg_export_cb'])

        # One row per background transition, and each records its own owner.
        self.assert_related_count(TransitionMessage.objects.all(), 2)
        messages = list(TransitionMessage.objects.order_by('id'))
        self.assertEqual([t.transition_name for t in messages],
                         ['bg_fulfil', 'bg_export'])
        self.assertTrue(all(t.is_completed for t in messages))
        self.assert_transition_owner(
            widget, _BG_CHAIN, transition_name='bg_fulfil'
        )
        self.assert_transition_owner(
            widget, _BG_CHAIN, transition_name='bg_export'
        )

    def test_journey_pins_the_whole_transformation(self):
        # One statement pins the whole transformation: draft to exported, both
        # side-effects, the export callback, and no failure.
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

    def test_failure_of_first_step_does_not_chain(self):
        # A failed first step stops the chain. The follow-up never runs and no
        # second row is saved. The instance waits in the first in-progress
        # state for the retry.
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'bg_fulfil', fail_side_effect='se_bg_fulfil_se',
            fail_with=ValueError('fulfil broke'),
        )
        self.assert_state(widget, 'chain_fulfilling')
        # Only the first step's side-effect ran, and it was injected to fail.
        self.assert_side_effects_not_ran(['se_bg_export_se'])
        # One row only: the failed first step, uncompleted so it is retried.
        self.assertEqual(TransitionMessage.objects.count(), 1)
        self.assertFalse(TransitionMessage.objects.get().is_completed)
        self.assert_transition_owner(widget, _BG_CHAIN, transition_name='bg_fulfil')

    def test_terminal_failure_of_first_step_does_not_chain(self):
        # When the first step uses up MAX_ERRORS it moves to its failed_state.
        # The follow-up still never runs.
        widget = self.create_instance(status='draft')
        self.background_transition(
            widget, 'bg_fulfil', fail_side_effect='se_bg_fulfil_se',
            fail_with=ValueError('persistent'),
        )
        # Retry until the row is terminal.
        for _ in range(2):  # MAX_ERRORS is 3: the first attempt plus 2 retries
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

        # Each leg's row records the NESTED Gmail class as owner — not the
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


_CHAIN_SEEN: dict = {}


def _record_chain_kwargs(instance, **kwargs):
    _CHAIN_SEEN.clear()
    _CHAIN_SEEN.update(kwargs)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class SyncToBackgroundRequestChainTests(TestCase):
    """A sync transition's next_transition into a BACKGROUND follow-up
    must not forward ``request`` — the follow-up's refusal at enqueue is
    swallowed by NextTransition, silently killing the chain. Sync
    follow-ups keep receiving request."""

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

    def test_background_follow_up_runs_despite_request(self):
        self.widget.request_chain_process.kick(request=object())
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'done')
        self.assertNotIn('request', _CHAIN_SEEN)

    def test_background_follow_up_runs_despite_user_id(self):
        # user_id is refused at a direct enqueue (it is the engine's wire
        # form for user), but it is ordinary data on the synchronous
        # transition that chains — so the hop strips it the same way it
        # strips request, instead of silently killing the chain.
        self.widget.request_chain_process.kick(user_id=42)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'done')
        self.assertNotIn('user_id', _CHAIN_SEEN)

    def test_sync_follow_up_still_receives_request(self):
        sentinel = object()
        self.widget.request_chain_process.kick_sync(request=sentinel)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'done')
        self.assertIs(_CHAIN_SEEN.get('request'), sentinel)
