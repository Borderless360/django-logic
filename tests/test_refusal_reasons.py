"""Reason metadata explains existing refusals without changing execution."""
import json
import pickle
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from django_logic import Process, Transition, conf
from django_logic.background import BackgroundTransition
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from django_logic.exceptions import (
    RefusalReason, TransitionNotAllowed, TransitionTemporarilyUnavailable,
)
from tests import dl_settings
from tests.models import Invoice


def deny(instance, **kwargs):
    return False


def deny_permission(instance, user, **kwargs):
    return False


class ReasonValueTests(SimpleTestCase):
    def test_plain_caller_exceptions_keep_args_and_have_no_invented_reason(self):
        error = TransitionNotAllowed('old text', 17)
        self.assertEqual(error.args, ('old text', 17))
        self.assertIsNone(error.reason)
        self.assertIsNone(error.current_state)
        self.assertIsNone(error.available_actions)

    def test_reason_is_json_serializable_and_survives_exception_pickle(self):
        error = AlreadyInProgress('old text', reason=RefusalReason.BACKGROUND_IN_FLIGHT)
        restored = pickle.loads(pickle.dumps(error))
        self.assertIsInstance(restored, TransitionTemporarilyUnavailable)
        self.assertIsInstance(restored, TransitionNotAllowed)
        self.assertEqual(restored.args, ('old text',))
        self.assertEqual(restored.reason, 'background_in_flight')
        self.assertEqual(json.dumps({'reason': restored.reason}),
                         '{"reason": "background_in_flight"}')


class ResolverReasonTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.invoice = Invoice.objects.create(status='draft')

    def process(self, process_class):
        return process_class(instance=self.invoice, field_name='status')

    def refusal(self, call, reason):
        with self.assertRaises(TransitionNotAllowed) as caught:
            call()
        error = caught.exception
        self.assertIs(type(error), TransitionNotAllowed)
        self.assertEqual(error.reason, reason)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')
        return error

    def test_transition_permission_short_circuits_condition_with_no_extra_calls(self):
        permission = Mock(return_value=False)
        condition = Mock(return_value=False)

        class Workflow(Process):
            transitions = [Transition('go', sources=['draft'], permissions=[permission], conditions=[condition])]

        error = self.refusal(lambda: self.process(Workflow).go(user=object()), RefusalReason.PERMISSION)
        self.assertEqual(permission.call_count, 2)  # Resolution, then the existing action hint.
        condition.assert_not_called()
        self.assertEqual(error.current_state, 'draft')
        self.assertEqual(error.available_actions, [])
        self.assertIn("The instance is in state 'draft'; available actions: none.", str(error))

    def test_condition_is_not_rerun_to_compute_the_reason(self):
        condition = Mock(return_value=False)

        class Workflow(Process):
            transitions = [Transition('go', sources=['draft'], conditions=[condition])]

        self.refusal(lambda: self.process(Workflow).go(), RefusalReason.CONDITION)
        self.assertEqual(condition.call_count, 2)

    def test_success_runs_predicates_once(self):
        condition = Mock(return_value=True)
        permission = Mock(return_value=True)
        effect = Mock()

        class Workflow(Process):
            transitions = [Transition('go', sources=['draft'], permissions=[permission],
                                      conditions=[condition], side_effects=[effect])]

        self.process(Workflow).go(user=object())
        self.assertEqual(permission.call_count, 1)
        self.assertEqual(condition.call_count, 1)
        self.assertEqual(effect.call_count, 1)

    def test_source_rejection_does_not_evaluate_transition_guards(self):
        condition = Mock(return_value=False)
        permission = Mock(return_value=False)

        class Workflow(Process):
            transitions = [Transition('go', sources=['approved'], permissions=[permission], conditions=[condition])]

        self.refusal(lambda: self.process(Workflow).go(user=object()), RefusalReason.SOURCE_STATE)
        condition.assert_not_called()
        permission.assert_not_called()

    def test_process_guard_explains_a_blocked_nested_action(self):
        class Child(Process):
            transitions = [Transition('go', sources=['draft'])]

        class PermissionWorkflow(Process):
            permissions = [deny_permission]
            nested_processes = [Child]

        class ConditionWorkflow(Process):
            conditions = [deny]
            nested_processes = [Child]

        self.refusal(lambda: self.process(PermissionWorkflow).go(user=object()), RefusalReason.PERMISSION)
        self.refusal(lambda: self.process(ConditionWorkflow).go(), RefusalReason.CONDITION)

    def test_unknown_name_does_not_inherit_an_unrelated_process_guard(self):
        class Workflow(Process):
            conditions = [deny]
            transitions = [Transition('go', sources=['draft'])]

        self.refusal(lambda: self.process(Workflow).missing_action(), RefusalReason.UNKNOWN_ACTION)

    def test_first_matching_branch_explains_refusal_without_unrelated_guards(self):
        class Unrelated(Process):
            permissions = [deny_permission]
            transitions = [Transition('other', sources=['draft'])]

        class First(Process):
            transitions = [Transition('go', sources=['draft'], conditions=[deny])]

        class Second(Process):
            transitions = [Transition('go', sources=['draft'], permissions=[deny_permission])]

        class Workflow(Process):
            nested_processes = [Unrelated, First, Second]

        self.refusal(lambda: self.process(Workflow).go(user=object()), RefusalReason.CONDITION)

    def test_ambiguous_action_keeps_message_and_class(self):
        class Workflow(Process):
            transitions = [Transition('go', sources=['draft']), Transition('go', sources=['draft'])]

        error = self.refusal(lambda: self.process(Workflow).go(), RefusalReason.AMBIGUOUS)
        self.assertEqual(str(error), 'There are several transitions available')

    def test_action_hint_cannot_replace_the_resolved_reason(self):
        def broken_hint(instance, **kwargs):
            raise ValueError('cannot list this action')

        class Workflow(Process):
            transitions = [Transition('go', sources=['draft'], conditions=[deny]),
                           Transition('other', sources=['draft'], conditions=[broken_hint])]

        error = self.refusal(lambda: self.process(Workflow).go(), RefusalReason.CONDITION)
        self.assertIsNone(error.available_actions)

    def test_custom_validity_overrides_are_called_and_not_guessed(self):
        class CustomTransition(Transition):
            def is_valid(self, instance, user=None):
                return False

        class TransitionWorkflow(Process):
            transitions = [CustomTransition('go', sources=['draft'])]

        class ProcessWorkflow(Process):
            transitions = [Transition('go', sources=['draft'])]

            def is_valid(self, user=None):
                return False

        self.refusal(lambda: self.process(TransitionWorkflow).go(), None)
        self.refusal(lambda: self.process(ProcessWorkflow).go(), None)

    def test_user_required_override_can_delegate_to_stock_checks(self):
        class UserRequired(Transition):
            def is_valid(self, instance, user=None):
                return user is not None and super().is_valid(instance, user)

        class Workflow(Process):
            transitions = [UserRequired('go', sources=['draft'], conditions=[deny])]

        self.refusal(lambda: self.process(Workflow).go(), None)
        self.refusal(lambda: self.process(Workflow).go(user=object()), RefusalReason.CONDITION)

    def test_nested_resolver_does_not_replace_the_outer_condition_reason(self):
        class Inner(Process):
            transitions = [Transition('go', sources=['draft'], permissions=[deny_permission])]

        def nested_refusal(instance, **kwargs):
            self.refusal(lambda: self.process(Inner).go(user=object()), RefusalReason.PERMISSION)
            return False

        class Outer(Process):
            transitions = [Transition('go', sources=['draft'], conditions=[nested_refusal])]

        self.refusal(lambda: self.process(Outer).go(), RefusalReason.CONDITION)
        self.refusal(lambda: self.process(Outer).missing_action(), RefusalReason.UNKNOWN_ACTION)

    def test_opaque_override_cannot_inherit_another_objects_validity_reason(self):
        other = Transition('other', sources=['draft'], permissions=[deny_permission])

        class Opaque(Transition):
            def is_valid(self, instance, user=None):
                other.is_valid(instance, user)
                return False

        class Workflow(Process):
            transitions = [Opaque('go', sources=['draft'])]

        self.refusal(lambda: self.process(Workflow).go(user=object()), None)

    def test_raising_predicate_does_not_leak_capture_to_the_next_call(self):
        def broken(instance, **kwargs):
            raise ValueError('guard failed')

        class Workflow(Process):
            transitions = [Transition('go', sources=['draft'], conditions=[broken]),
                           Transition('later', sources=['approved'])]

        with self.assertRaisesMessage(ValueError, 'guard failed'):
            self.process(Workflow).go()
        self.refusal(lambda: self.process(Workflow).later(), RefusalReason.SOURCE_STATE)


class ConcurrentReasonTests(SimpleTestCase):
    def test_shared_transition_keeps_each_callers_reason(self):
        barrier = Barrier(2, timeout=5)

        class PausedTransition(Transition):
            def is_valid(self, instance, user=None):
                valid = super().is_valid(instance, user)
                barrier.wait()
                return valid

        def permission(instance, user, **kwargs):
            return user == 'condition'

        class Workflow(Process):
            transitions = [PausedTransition('go', sources=['draft'], permissions=[permission], conditions=[deny])]

        def refuse(user):
            instance = Invoice(pk=123, status='draft')
            process = Workflow(instance=instance, field_name='status')
            try:
                process.go(user=user)
            except TransitionNotAllowed as error:
                return error.reason
            self.fail('The action must be refused.')

        with ThreadPoolExecutor(max_workers=2) as executor:
            reasons = list(executor.map(refuse, ['permission', 'condition']))
        self.assertEqual(reasons, [RefusalReason.PERMISSION, RefusalReason.CONDITION])


@override_settings(DJANGO_LOGIC=dl_settings(TRANSITION_MESSAGE_RETRY_MINUTES=2))
class ExecutionGateReasonTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.invoice = Invoice.objects.create(status='draft')

        class Workflow(Process):
            transitions = [Transition('touch', sources=['draft']),
                           BackgroundTransition('work', sources=['draft'])]

        self.process = Workflow(instance=self.invoice, field_name='status')

    def pending(self):
        return TransitionMessage.objects.create(
            **TransitionMessage.instance_key(self.invoice, self.process.process_name),
            field_name='status', transition_name='work', kwargs={},
        )

    def test_lock_refusals_preserve_plain_class_and_message_on_both_paths(self):
        self.assertTrue(self.process.state.lock())
        self.addCleanup(self.process.state.unlock)
        for call in (self.process.touch, self.process.work):
            with self.subTest(call=call), self.assertRaises(TransitionNotAllowed) as caught:
                call()
            self.assertIs(type(caught.exception), TransitionNotAllowed)
            self.assertEqual(str(caught.exception), 'State is locked')
            self.assertEqual(caught.exception.reason, RefusalReason.LOCKED)
        self.assertEqual(TransitionMessage.objects.count(), 0)

    def test_pending_work_preserves_transient_and_duplicate_classes(self):
        self.pending()
        for call, expected_class in [(self.process.touch, TransitionTemporarilyUnavailable),
                                     (self.process.work, AlreadyInProgress)]:
            with self.subTest(expected_class=expected_class), self.assertRaises(TransitionNotAllowed) as caught:
                call()
            self.assertIs(type(caught.exception), expected_class)
            self.assertEqual(caught.exception.reason, RefusalReason.BACKGROUND_IN_FLIGHT)
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_stranded_work_preserves_plain_class_on_both_paths(self):
        pending = self.pending()
        TransitionMessage.objects.filter(pk=pending.pk).update(
            modified=timezone.now() - timedelta(minutes=conf.retry_window_minutes() + 1))
        for call in (self.process.touch, self.process.work):
            with self.subTest(call=call), self.assertRaises(TransitionNotAllowed) as caught:
                call()
            self.assertIs(type(caught.exception), TransitionNotAllowed)
            self.assertEqual(caught.exception.reason, RefusalReason.BACKGROUND_STRANDED)
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_persisted_source_recheck_has_reason_and_releases_lock(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(status='changed')
        for call in (self.process.touch, self.process.work):
            with self.subTest(call=call), self.assertRaises(TransitionNotAllowed) as caught:
                call()
            self.assertIs(type(caught.exception), TransitionNotAllowed)
            self.assertEqual(caught.exception.reason, RefusalReason.SOURCE_STATE)
            self.assertFalse(self.process.state.is_locked())
        self.assertEqual(TransitionMessage.objects.count(), 0)
