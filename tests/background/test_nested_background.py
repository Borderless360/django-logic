"""Regression: background transitions declared on a *nested* process.

Phase 1 can start a background transition that lives on a nested process —
``get_transition_by_action_name`` descends into ``nested_processes``. The
``TransitionMessage`` records only the bound (parent) ``process_name``, so
phase 2 restores the parent process and must descend into ``nested_processes``
itself to find the transition (``runner._find_transition``).

Before that descent existed, phase 2 could not locate the nested transition:
the message was marked completed, the side-effects never ran, and the instance
was stranded in ``in_progress_state``. Every test here fails on that old code.
"""
from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from tests.background.models import Widget


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 3,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class NestedBackgroundTransitionTests(TestCase):
    def setUp(self):
        self.widget = Widget.objects.create(status='draft')

    def test_reaches_target_and_runs_side_effects(self):
        # Invoked through the PARENT property; the transition lives on the
        # nested child process.
        tr_id = self.widget.parent_process.nested_fulfil()
        self.assertIsNotNone(tr_id)

        self.widget.refresh_from_db()
        # Old behaviour: stuck in 'nested_fulfilling', se_log empty.
        self.assertEqual(self.widget.status, 'nested_fulfilled')
        self.assertIn('ok,', self.widget.se_log)
        self.assertIn('cb,', self.widget.cb_log)
        self.assertNotIn('fcb,', self.widget.cb_log)

    def test_transition_message_completed(self):
        self.widget.parent_process.nested_fulfil()
        tm = TransitionMessage.objects.get(transition_name='nested_fulfil')
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.errors_count, 0)
        self.assertEqual(tm.queue_name, 'django_logic.critical')
        # The message records the bound parent process, not the nested one —
        # which is exactly why phase 2 has to descend.
        self.assertEqual(tm.process_name, 'parent_process')

    def test_two_levels_deep(self):
        # nested_processes -> NestedBgMidProcess -> NestedBgGrandchildProcess
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
        # The clincher: pre-fix the nested transition was never found, so the
        # raising side-effect never ran and nothing was raised. Post-fix the
        # side-effect runs and (sync mode) the exception propagates.
        with self.assertRaises(ValueError):
            self.widget.parent_process.nested_crash()

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'nested_crashing')
        tm = TransitionMessage.objects.get(transition_name='nested_crash')
        self.assertFalse(tm.is_completed)
        self.assertEqual(tm.errors_count, 1)
