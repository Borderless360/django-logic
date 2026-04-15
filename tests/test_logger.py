from django.test import TestCase
from django.core.cache import cache
from django_logic import Transition, Action
from django_logic.logger import TransitionEventType
from django_logic.state import State
from django_logic.exceptions import TransitionNotAllowed
from tests.models import Invoice
from tests.utils import get_test_logger


def disable_invoice(invoice: Invoice, *args, **kwargs):
    invoice.is_available = False
    invoice.save()


def enable_invoice(invoice: Invoice, *args, **kwargs):
    invoice.is_available = True
    invoice.save()


def fail_invoice(invoice: Invoice, *args, **kwargs):
    raise Exception("Test exception")


class TransitionLoggingTestCase(TestCase):

    def setUp(self) -> None:
        cache.clear()
        self.invoice = Invoice.objects.create(status='draft')
        self.logs = get_test_logger()
        self.logs.clear()
    
    def test_transition_locks_state_logging(self):
        """Test that locking state is logged."""
        transition = Transition('test', sources=[], target='cancelled')
        state = State(self.invoice, 'status')

        transition.change_state(state)

        # Check that state locking was logged (message contains Lock)
        lock_logs = [log for log in self.logs.get_logs() if 'Lock' in log['message']]
        self.assertGreater(len(lock_logs), 0)

    def test_transition_completed_logging(self):
        """Test that successful transition completion is logged."""
        transition = Transition('test', sources=[], target='cancelled')
        state = State(self.invoice, 'status')

        transition.change_state(state)

        # Check for Set State log (state changed to cancelled)
        completed_logs = [log for log in self.logs.get_logs()
                         if 'Set State' in log['message'] and 'cancelled' in log['message']]
        self.assertEqual(len(completed_logs), 1)
        self.assertIn('Set State', completed_logs[0]['message'])
        self.assertIn('cancelled', completed_logs[0]['message'])

    def test_transition_failed_logging(self):
        """Test that failed transition is logged."""
        transition = Transition(
            'test',
            sources=[],
            target='success',
            failed_state='failed',
            side_effects=[fail_invoice]
        )
        state = State(self.invoice, 'status')

        with self.assertRaises(Exception):
            transition.change_state(state)

        # Check for Set State log (state changed to failed)
        failed_logs = [log for log in self.logs.get_logs()
                      if 'Set State' in log['message'] and 'failed' in log['message']]
        self.assertEqual(len(failed_logs), 1)
        self.assertIn('Set State', failed_logs[0]['message'])
        self.assertIn('failed', failed_logs[0]['message'])

    def test_transition_error_logging(self):
        """Test that errors during side effects are logged."""
        transition = Transition(
            'test',
            sources=[],
            target='success',
            failed_state='failed',
            side_effects=[fail_invoice]
        )
        state = State(self.invoice, 'status')

        with self.assertRaises(Exception):
            transition.change_state(state)

        # Check for error logs (level should be ERROR)
        error_logs = [log for log in self.logs.get_logs() if log.get('level') == 'ERROR']
        self.assertGreater(len(error_logs), 0)
        self.assertEqual(error_logs[0]['level'], 'ERROR')

    def test_side_effects_started_logging(self):
        """Test that side effects start is logged."""
        transition = Transition(
            'test',
            sources=[],
            target='cancelled',
            side_effects=[disable_invoice]
        )
        state = State(self.invoice, 'status')

        transition.change_state(state)

        # Check for side effects started log
        self.assertTrue(self.logs.has_log('SideEffect'))
        side_effect_logs = [log for log in self.logs.get_logs() if 'SideEffect' in log['message']]
        self.assertGreater(len(side_effect_logs), 0)

    def test_side_effects_succeeded_logging(self):
        """Test that successful side effects are logged."""
        transition = Transition(
            'test',
            sources=[],
            target='cancelled',
            side_effects=[disable_invoice]
        )
        state = State(self.invoice, 'status')

        transition.change_state(state)

        # Check for side effects succeeded log (SideEffect in message)
        self.assertTrue(self.logs.has_log('SideEffect'))
        side_effect_logs = [log for log in self.logs.get_logs() if 'SideEffect' in log['message']]
        self.assertGreater(len(side_effect_logs), 0)

    def test_side_effects_failed_logging(self):
        """Test that failed side effects are logged."""
        transition = Transition(
            'test',
            sources=[],
            target='success',
            failed_state='failed',
            side_effects=[fail_invoice]
        )
        state = State(self.invoice, 'status')

        with self.assertRaises(Exception):
            transition.change_state(state)

        # Check for side effects failed log (SideEffect in message) and error log
        self.assertTrue(self.logs.has_log('SideEffect'))
        side_effect_logs = [log for log in self.logs.get_logs() if 'SideEffect' in log['message']]
        self.assertGreater(len(side_effect_logs), 0)
        error_logs = [log for log in self.logs.get_logs() if log.get('level') == 'ERROR']
        self.assertGreater(len(error_logs), 0)

    def test_callbacks_failed_logging(self):
        """Test that failed callbacks are logged."""
        transition = Transition(
            'test',
            sources=[],
            target='cancelled',
            callbacks=[fail_invoice]
        )
        state = State(self.invoice, 'status')

        transition.change_state(state)

        self.assertTrue(self.logs.has_log('Callbacks'))
        callback_logs = [log for log in self.logs.get_logs() if 'Callback' in log['message']]
        self.assertGreater(len(callback_logs), 0)
        # Should also have error logs when a callback raises
        error_logs = [log for log in self.logs.get_logs() if log.get('level') == 'ERROR']
        self.assertGreater(len(error_logs), 0)

    def test_transition_unlock_logging(self):
        """Test that unlocking state is logged."""
        transition = Transition('test', sources=[], target='cancelled')
        state = State(self.invoice, 'status')

        transition.change_state(state)

        # Check that state unlocking was logged (message contains Unlock)
        unlock_logs = [log for log in self.logs.get_logs() if 'Unlock' in log['message']]
        self.assertGreater(len(unlock_logs), 0)

    def test_locked_state_logging(self):
        """Test that attempting transition on locked state raises TransitionNotAllowed."""
        transition = Transition('test', sources=[], target='cancelled')
        state = State(self.invoice, 'status')
        state.lock()

        with self.assertRaises(TransitionNotAllowed):
            transition.change_state(state)

        # Transition was rejected before any state change; no Set State log for target
        completed_logs = [log for log in self.logs.get_logs()
                         if 'Set State' in log['message'] and 'cancelled' in log['message']]
        self.assertEqual(len(completed_logs), 0)

    def test_in_progress_state_logging(self):
        """Test that in-progress state change is logged."""
        transition = Transition(
            'test',
            sources=[],
            target='cancelled',
            in_progress_state='processing'
        )
        state = State(self.invoice, 'status')

        transition.change_state(state)

        # Check for in-progress state log (message contains Set State and processing)
        in_progress_logs = [log for log in self.logs.get_logs()
                           if 'Set State' in log['message'] and 'processing' in log['message']]
        self.assertGreater(len(in_progress_logs), 0)
        self.assertIn('Set State', in_progress_logs[0]['message'])
        self.assertIn('processing', in_progress_logs[0]['message'])

    def test_log_data_structure(self):
        """Test that log messages contain expected content."""
        transition = Transition('test', sources=[], target='cancelled')
        state = State(self.invoice, 'status')

        transition.change_state(state)

        all_logs = self.logs.get_logs()
        self.assertGreater(len(all_logs), 0)

        # Check that Start is logged and message contains transition name and instance key
        start_logs = [log for log in all_logs if TransitionEventType.START.value in log['message']]
        self.assertGreater(len(start_logs), 0)
        start_message = start_logs[0]['message']
        self.assertIn(self.invoice._meta.model_name, start_message)
        self.assertIn('status', start_message)
        self.assertIn('test', start_message)
        self.assertIn(str(self.invoice.pk), start_message)


class ActionLoggingTestCase(TestCase):

    def setUp(self) -> None:
        cache.clear()
        self.invoice = Invoice.objects.create(status='draft')
        self.logs = get_test_logger()
        self.logs.clear()

    def test_action_side_effects_logging(self):
        """Test that action side effects are logged."""
        action = Action(
            'test',
            sources=['draft'],
            side_effects=[disable_invoice]
        )
        state = State(self.invoice, 'status')

        action.change_state(state)

        # Check for side effects logs (message contains SideEffect)
        self.assertTrue(self.logs.has_log('SideEffect'))
        side_effect_logs = [log for log in self.logs.get_logs() if 'SideEffect' in log['message']]
        self.assertGreater(len(side_effect_logs), 0)

    def test_action_failed_logging(self):
        """Test that failed action is logged."""
        action = Action(
            'test',
            sources=['draft'],
            failed_state='failed',
            side_effects=[fail_invoice]
        )
        state = State(self.invoice, 'status')

        with self.assertRaises(Exception):
            action.change_state(state)

        # Check for Set State log (state changed to failed)
        failed_logs = [log for log in self.logs.get_logs()
                      if 'Set State' in log['message'] and 'failed' in log['message']]
        self.assertEqual(len(failed_logs), 1)
        self.assertIn('Set State', failed_logs[0]['message'])
        self.assertIn('failed', failed_logs[0]['message'])

    def test_action_error_logging(self):
        """Test that errors during action side effects are logged."""
        action = Action(
            'test',
            sources=['draft'],
            failed_state='failed',
            side_effects=[fail_invoice]
        )
        state = State(self.invoice, 'status')

        with self.assertRaises(Exception):
            action.change_state(state)

        # Check for error logs (level should be ERROR)
        error_logs = [log for log in self.logs.get_logs() if log.get('level') == 'ERROR']
        self.assertGreater(len(error_logs), 0)
        self.assertEqual(error_logs[0]['level'], 'ERROR')


