"""Background transitions declared on a nested process.

The row records only the bound parent ``process_name``, so the worker restores
the parent process and has to descend into ``nested_processes`` to find the
transition. Without that descent the worker marked the row completed, ran no
side-effects, and left the instance in its ``in_progress_state``.
"""
from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from tests.background.models import Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class NestedBackgroundTransitionTests(TestCase):
    def setUp(self):
        self.widget = Widget.objects.create(status='draft')

    def test_reaches_target_and_runs_side_effects(self):
        # Called through the parent property, while the transition lives on the
        # nested child process.
        tr_id = self.widget.parent_process.nested_fulfil()
        self.assertIsNotNone(tr_id)

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'nested_fulfilled')
        self.assertIn('ok,', self.widget.se_log)
        self.assertIn('cb,', self.widget.cb_log)
        self.assertNotIn('fcb,', self.widget.cb_log)

    def test_transition_message_completed(self):
        self.widget.parent_process.nested_fulfil()
        row = TransitionMessage.objects.get(transition_name='nested_fulfil')
        self.assertTrue(row.is_completed)
        self.assertEqual(row.errors_count, 0)
        self.assertEqual(row.queue_name, 'django_logic.critical')
        # The row records the bound parent process, not the nested one, which
        # is why the worker has to descend.
        self.assertEqual(row.process_name, 'parent_process')

    def test_two_levels_deep(self):
        # The descent goes through NestedBgMidProcess to
        # NestedBgGrandchildProcess.
        self.widget.parent_process.deeply_nested_fulfil()
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'deeply_nested_fulfilled')
        self.assertIn('cb,', self.widget.cb_log)
        self.assertTrue(
            TransitionMessage.objects.get(
                transition_name='deeply_nested_fulfil'
            ).is_completed
        )

    def test_nested_background_action_runs_without_state_change(self):
        self.widget.status = 'nested_fulfilled'
        self.widget.save(update_fields=['status'])

        self.widget.parent_process.nested_sync_inventory()
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'nested_fulfilled')  # unchanged
        self.assertIn('ok,', self.widget.se_log)
        self.assertIn('cb,', self.widget.cb_log)
        self.assertTrue(
            TransitionMessage.objects.get(
                transition_name='nested_sync_inventory'
            ).is_completed
        )

    def test_nested_failure_actually_runs_side_effect(self):
        # Without the descent the raising side-effect never ran, so nothing was
        # raised and the test would have looked like a pass. In sync mode the
        # side-effect runs and the exception propagates.
        with self.assertRaises(ValueError):
            self.widget.parent_process.nested_crash()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'nested_crashing')
        row = TransitionMessage.objects.get(transition_name='nested_crash')
        self.assertFalse(row.is_completed)
        self.assertEqual(row.errors_count, 1)
