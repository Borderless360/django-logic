from unittest.mock import patch

from django.test import TestCase

from django_logic.state import State
from django_logic import Transition, Action, Process
from tests.models import Invoice


def disable_invoice(invoice: Invoice, *args, **kwargs):
    invoice.is_available = False
    invoice.save()


def update_invoice(invoice, is_available, customer_received, *args, **kwargs):
    invoice.is_available = is_available
    invoice.customer_received = customer_received
    invoice.save()


def enable_invoice(invoice: Invoice, *args, **kwargs):
    invoice.is_available = True
    invoice.save()


def fail_invoice(invoice: Invoice, *args, **kwargs):
    raise Exception


def receive_invoice(invoice: Invoice, *args, **kwargs):
    invoice.customer_received = True
    invoice.save()


def debug_action(*args, **kwargs):
    pass


class TransitionSideEffectsTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_side_effect(self):
        transition = Transition('test', sources=[], target='cancelled', side_effects=[disable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, transition.target)
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_many_side_effects(self):
        transition = Transition('test', sources=[], target='cancelled',
                                side_effects=[disable_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, transition.target)
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_side_effect(self):
        transition = Transition('test', sources=[], target='cancelled',
                                side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_side_effect_with_failed_state(self):
        transition = Transition('test', sources=[], target='cancelled', failed_state='failed',
                                side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_side_effect_with_parameters(self):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        transition = Transition('test', sources=[], target='cancelled', failed_state='failed',
                                side_effects=[update_invoice])
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        transition.change_state(state, is_available=False, customer_received=False)
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        self.assertFalse(state.is_locked())


class TransitionCallbacksTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_callback(self):
        transition = Transition('test', sources=[], target='cancelled', callbacks=[disable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, transition.target)
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_many_callbacks(self):
        transition = Transition('test', sources=[], target='cancelled',
                                callbacks=[disable_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, transition.target)
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_callbacks(self):
        transition = Transition('test', sources=[], target='cancelled',
                                callbacks=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'cancelled')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_callbacks_with_failed_state(self):
        transition = Transition('test', sources=[], target='cancelled', failed_state='failed',
                                side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_callbacks_with_parameters(self):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        transition = Transition('test', sources=[], target='cancelled', failed_state='failed',
                                callbacks=[update_invoice])
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        transition.change_state(state, is_available=False, customer_received=False)
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        self.assertFalse(state.is_locked())


class TransitionFailureCallbacksTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_callback(self):
        transition = Transition('test', sources=[], target='success', side_effects=[fail_invoice],
                                failure_callbacks=[disable_invoice], failed_state='failed')
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_many_callback(self):
        transition = Transition('test', sources=[], target='success', side_effects=[fail_invoice],
                                failure_callbacks=[disable_invoice, receive_invoice], failed_state='failed')
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        transition.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        self.assertFalse(state.is_locked())

    def test_callbacks_with_parameters(self):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        transition = Transition('test', sources=[], target='success', failed_state='failed',
                                side_effects=[fail_invoice], failure_callbacks=[update_invoice])
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        transition.change_state(state, is_available=False, customer_received=False)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        self.assertFalse(state.is_locked())

    @patch('tests.test_transition.debug_action')
    def test_failure_callback_exception_passed(self, debug_mock):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        transition = Transition('test', sources=[], target='success', failed_state='failed',
                                side_effects=[fail_invoice], failure_callbacks=[debug_action])
        self.invoice.refresh_from_db()
        state = State(self.invoice, 'status')
        transition.change_state(state, foo="bar")
        self.assertTrue(debug_mock.called)
        self.assertEqual(debug_mock.call_count, 1)
        call_args = debug_mock.call_args[0]
        call_kwargs = debug_mock.call_args[1]
        self.assertEqual(call_args, (self.invoice,))
        self.assertEqual(len(call_kwargs), 3)
        self.assertTrue(isinstance(call_kwargs['exception'], Exception))
        self.assertEqual(call_kwargs['foo'], 'bar')


class ActionSideEffectsTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_side_effect(self):
        action = Action('test', sources=['draft'], side_effects=[disable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        action.change_state(state)
        self.assertEqual(self.invoice.status, 'draft')  # not changed
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_many_side_effects(self):
        action = Action('test', sources=['draft'], side_effects=[disable_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        action.change_state(state)
        self.assertEqual(self.invoice.status, 'draft')
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_side_effect(self):
        action = Action('test', sources=['draft'], side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        action.change_state(state)
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_side_effect_with_failed_state(self):
        action = Action('test', sources=['draft'], failed_state='failed', side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        action.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_side_effect_with_parameters(self):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        action = Action('test', sources=['draft'], failed_state='failed', side_effects=[update_invoice])
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        action.change_state(state, is_available=False, customer_received=False)
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        self.assertFalse(state.is_locked())


class ActionCallbacksTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_callback(self):
        action = Action('test', sources=['draft'], callbacks=[disable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        action.change_state(state)
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_many_callbacks(self):
        action = Action('test', sources=['draft'], callbacks=[disable_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        action.change_state(state)
        self.assertEqual(self.invoice.status, 'draft')
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_callbacks(self):
        action = Action('test', sources=['draft'], callbacks=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        action.change_state(state)
        self.assertEqual(self.invoice.status, 'draft')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_failure_during_callbacks_with_failed_state(self):
        action = Action('test', failed_state='failed', sources=['draft'], side_effects=[disable_invoice, fail_invoice, enable_invoice])
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        action.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_callbacks_with_parameters(self):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        action = Action('test', failed_state='failed', sources=['draft'], callbacks=[update_invoice])
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        action.change_state(state, is_available=False, customer_received=False)
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        self.assertFalse(state.is_locked())


class ActionFailureCallbacksTestCase(TestCase):
    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status='draft')

    def test_one_callback(self):
        action = Action('test', side_effects=[fail_invoice], sources=['draft'],
                        failure_callbacks=[disable_invoice], failed_state='failed')
        self.assertTrue(self.invoice.is_available)
        state = State(self.invoice, 'status')
        action.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(state.is_locked())

    def test_many_callback(self):
        action = Action('test', side_effects=[fail_invoice], sources=['draft'],
                        failure_callbacks=[disable_invoice, receive_invoice], failed_state='failed')
        self.assertTrue(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        action.change_state(state)
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        self.assertFalse(state.is_locked())

    def test_callbacks_with_parameters(self):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        action = Action('test', failed_state='failed',
                        side_effects=[fail_invoice], sources=['draft'], failure_callbacks=[update_invoice])
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
        state = State(self.invoice, 'status')
        action.change_state(state, is_available=False, customer_received=False)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'failed')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        self.assertFalse(state.is_locked())

    @patch('tests.test_transition.debug_action')
    def test_failure_callback_exception_passed(self, debug_mock):
        update_invoice(self.invoice, is_available=True, customer_received=True)
        action = Action('test', failed_state='failed',
                        side_effects=[fail_invoice], sources=['draft'], failure_callbacks=[debug_action])
        self.invoice.refresh_from_db()
        state = State(self.invoice, 'status')
        action.change_state(state, foo="bar")
        self.assertTrue(debug_mock.called)
        self.assertEqual(debug_mock.call_count, 1)
        call_args = debug_mock.call_args[0]
        call_kwargs = debug_mock.call_args[1]
        self.assertEqual(call_args, (self.invoice,))
        self.assertEqual(len(call_kwargs), 3)
        self.assertTrue(isinstance(call_kwargs['exception'], Exception))
        self.assertEqual(call_kwargs['foo'], 'bar')


class TransitionNextTransitionTestCase(TestCase):
    TRANSITION_NAME = 'transition_test'
    NEXT_TRANSITION_NAME = 'next_transition_test'

    def setUp(self) -> None:
        self.invoice = Invoice.objects.create(status=Invoice.STATUS_DRAFT)
        self.state = State(self.invoice, 'status', 'process')

        # next transition
        self.next_transition = Transition(self.NEXT_TRANSITION_NAME,
                                          sources=[Invoice.STATUS_SUCCESS],
                                          target=Invoice.STATUS_CANCELLED)

        class TestProcess(Process):
            transitions = [
                self.next_transition,
            ]

        self.process = TestProcess(instance=self.invoice, state=self.state)
        self.invoice.process = self.process

    def test_successful_next_transition(self):
        transition1 = Transition(self.TRANSITION_NAME,
                                 sources=[Invoice.STATUS_DRAFT],
                                 target=Invoice.STATUS_SUCCESS,
                                 next_transition=self.NEXT_TRANSITION_NAME)
        self.process.transitions.append(transition1)

        with patch.object(self.next_transition, 'change_state') as next_transition_mock:
            transition1.change_state(self.state)

        next_transition_mock.assert_called_once()

    def test_failed_next_transition(self):
        transition1 = Transition(self.TRANSITION_NAME,
                                 sources=[Invoice.STATUS_DRAFT],
                                 target=Invoice.STATUS_SUCCESS,
                                 failed_state=Invoice.STATUS_FAILED,
                                 side_effects=[fail_invoice],
                                 next_transition=self.NEXT_TRANSITION_NAME)
        self.process.transitions.append(transition1)

        with patch.object(self.next_transition, 'change_state') as next_transition_mock:
            transition1.change_state(self.state)

        next_transition_mock.assert_not_called()


class InitTransitionContextTestCase(TestCase):
    def test_init_transition_context_test(self):
        initial_context = {'test_key': 1}
        Transition._init_transition_context(initial_context)
        self.assertEqual(initial_context['context'], {})
        self.assertEqual(initial_context['test_key'], 1)

    def test_existing_transition_context_test(self):
        initial_context = {
            'context': {'a': 1},
        }
        Transition._init_transition_context(initial_context)
        self.assertEqual(initial_context['context'], {'a': 1})
