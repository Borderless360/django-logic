"""The consumer-facing test helper that writes a coherent uncompleted row."""
from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.exceptions import (
    TransitionNotAllowed,
    TransitionTemporarilyUnavailable,
)
from django_logic.testing import open_transition_message
from tests.background.models import Conversation, Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class OpenTransitionMessageTests(TestCase):
    def test_the_row_gates_the_sync_path(self):
        widget = Widget.objects.create(status='draft')
        open_transition_message(widget, 'process', 'fulfil')
        with self.assertRaises(TransitionTemporarilyUnavailable):
            widget.process.cancel()

    def test_the_row_gates_a_second_enqueue(self):
        widget = Widget.objects.create(status='draft')
        open_transition_message(widget, 'process', 'fulfil')
        with self.assertRaises(TransitionNotAllowed):
            widget.process.fulfil()

    def test_the_independent_process_is_not_gated(self):
        widget = Widget.objects.create(status='fulfilled', audit_status='clean')
        open_transition_message(widget, 'process', 'generate_export')
        widget.audit_process.audit()
        widget.refresh_from_db()
        self.assertEqual(widget.audit_status, 'audited')

    def test_started_minutes_ago_ages_the_row(self):
        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(
            widget, 'process', 'fulfil', started_minutes_ago=60,
        )
        self.assertLess(
            (row.started_at - row.modified).total_seconds(), 1,
        )
        self.assertEqual(
            TransitionMessage.retry_status(widget, 'process'),
            TransitionMessage.STRANDED,
        )

    def test_the_row_records_current_identity_and_runs_on_retry(self):
        from django_logic.background.runner import run_background_transition

        widget = Widget.objects.create(status='fulfilling')
        row = open_transition_message(widget, 'process', 'fulfil')
        self.assertEqual(row.field_name, 'status')
        self.assertEqual(row.owning_process_class, 'tests.background.models.WidgetProcess')
        self.assertEqual(row.kwargs['process_class'], 'tests.background.models.WidgetProcess')
        run_background_transition(row.pk)
        widget.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(widget.status, 'fulfilled')
        self.assertTrue(row.is_completed)

    def test_a_wrong_state_field_is_rejected_before_saving(self):
        widget = Widget.objects.create(status='fulfilling')
        with self.assertRaisesMessage(ValueError, 'field_name must match'):
            open_transition_message(widget, 'process', 'fulfil', field_name='audit_status')
        self.assertFalse(TransitionMessage.objects.exists())

    def test_unknown_or_synchronous_transition_is_rejected_before_saving(self):
        widget = Widget.objects.create(status='draft')
        for action in ('missing', 'cancel'):
            with self.subTest(action=action):
                with self.assertRaisesMessage(ValueError, 'exactly one declared background'):
                    open_transition_message(widget, 'process', action)
        self.assertFalse(TransitionMessage.objects.exists())

    def test_ambiguous_nested_owner_is_not_guessed(self):
        conversation = Conversation.objects.create(status='open', source_integration='gmail')
        with self.assertRaisesMessage(ValueError, 'exactly one declared background'):
            open_transition_message(conversation, 'chain_conv', 'send')
        self.assertFalse(TransitionMessage.objects.exists())
