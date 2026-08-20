"""The two consumer-facing test helpers: a coherent uncompleted row, and a
record of which transitions a block of tests actually drove."""
from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.exceptions import (
    TransitionNotAllowed,
    TransitionTemporarilyUnavailable,
)
from django_logic.testing import open_transition_message, record_driven_transitions
from tests.background.models import Widget, WidgetProcess
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


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class RecordDrivenTransitionsTests(TestCase):
    def test_a_driven_transition_is_recorded(self):
        widget = Widget.objects.create(status='draft')
        with record_driven_transitions() as record:
            widget.process.fulfil()
        self.assertIn('fulfil', record.action_names)
        self.assertIn('cancel', record.undriven(WidgetProcess))
        self.assertNotIn('fulfil', record.undriven(WidgetProcess))

    def test_a_failed_side_effect_still_counts_as_driven(self):
        widget = Widget.objects.create(status='draft')
        with record_driven_transitions() as record:
            with self.assertRaises(ValueError):
                widget.process.crash()
        self.assertIn('crash', record.action_names)

    def test_a_refusal_does_not_count(self):
        widget = Widget.objects.create(status='fulfilled')
        with record_driven_transitions() as record:
            with self.assertRaises(TransitionNotAllowed):
                widget.process.fulfil()  # 'fulfilled' is not a source
        self.assertNotIn('fulfil', record.action_names)

    def test_the_wrap_is_restored_on_exit(self):
        from django_logic.process import Process

        original = Process._get_transition_method
        with record_driven_transitions():
            self.assertIsNot(Process._get_transition_method, original)
        self.assertIs(Process._get_transition_method, original)
