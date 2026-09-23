"""Process-owned messages preserve the existing refusal and check behavior."""
import inspect
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from django_logic import Process, Transition, commands, conf
from django_logic.background import BackgroundTransition
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.models import TransitionMessage
from django_logic.commands import StrictPermissions
from django_logic.exceptions import RefusalReason, TransitionNotAllowed, TransitionTemporarilyUnavailable
from tests import dl_settings
from tests.models import Invoice


def deny(instance, **kwargs):
    return False


def deny_permission(instance, user, **kwargs):
    return False


class RefusalMessageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.invoice = Invoice.objects.create(status='draft')

    def process(self, process_class):
        return process_class(instance=self.invoice, field_name='status')

    def refused(self, call):
        with self.assertRaises(TransitionNotAllowed) as caught:
            call()
        return caught.exception

    def test_default_condition_message_does_not_replace_diagnostics(self):
        class Workflow(Process):
            transitions = [Transition('go', sources=['draft'], conditions=[deny])]

        error = self.refused(self.process(Workflow).go)
        self.assertEqual(getattr(error, 'user_message', None),
                         "Action 'go' is not allowed: this record does not meet the requirements for this action.")
        self.assertIs(type(error), TransitionNotAllowed)
        self.assertEqual(error.reason, RefusalReason.CONDITION)
        self.assertEqual(error.current_state, 'draft')
        self.assertEqual(error.available_actions, [])
        self.assertEqual(error.args, (str(error),))
        self.assertIn('Process class', str(error))
        self.assertNotEqual(str(error), error.user_message)

    def test_process_override_and_generic_unknown_reason(self):
        class Workflow(Process):
            refusal_messages = {RefusalReason.CONDITION: 'Complete this invoice first.'}
            transitions = [Transition('go', sources=['draft'], conditions=[deny])]

        error = self.refused(self.process(Workflow).go)
        self.assertEqual(getattr(error, 'user_message', None), "Action 'go' is not allowed: Complete this invoice first.")

        class CustomTransition(Transition):
            def change_state(self, state, **kwargs):
                raise TransitionNotAllowed('technical detail', reason='future_reason')

        class FutureWorkflow(Process):
            transitions = [CustomTransition('go', sources=['draft'])]

        error = self.refused(self.process(FutureWorkflow).go)
        self.assertEqual(getattr(error, 'user_message', None), "Action 'go' is not allowed")
        self.assertEqual(str(error), 'technical detail')

    def test_nested_process_override_then_root_fallback(self):
        class PlainChild(Process):
            transitions = [Transition('go', sources=['draft'], conditions=[deny])]

        class CustomChild(Process):
            refusal_messages = {RefusalReason.CONDITION: 'Child requirements are missing.'}
            transitions = [Transition('go', sources=['draft'], conditions=[deny])]

        class RootFallback(Process):
            refusal_messages = {RefusalReason.CONDITION: 'Root requirements are missing.'}
            nested_processes = [PlainChild]

        class ChildOverride(Process):
            refusal_messages = {RefusalReason.CONDITION: 'Root requirements are missing.'}
            nested_processes = [CustomChild]

        self.assertEqual(getattr(self.refused(self.process(RootFallback).go), 'user_message', None),
                         "Action 'go' is not allowed: Root requirements are missing.")
        self.assertEqual(getattr(self.refused(self.process(ChildOverride).go), 'user_message', None),
                         "Action 'go' is not allowed: Child requirements are missing.")

    def test_transition_override_wins_over_both_process_maps_and_stays_literal(self):
        class Child(Process):
            refusal_messages = {RefusalReason.CONDITION: 'Child requirements.'}
            transitions = [Transition('go', sources=['draft'], conditions=[deny],
                                      refusal_messages={RefusalReason.CONDITION: 'Add {invoice}; keep {action_name}.'})]

        class Workflow(Process):
            refusal_messages = {RefusalReason.CONDITION: 'Root requirements.'}
            nested_processes = [Child]

        error = self.refused(self.process(Workflow).go)
        self.assertEqual(error.user_message, "Action 'go' is not allowed: Add {invoice}; keep {action_name}.")

    def test_first_failed_callable_wins_and_short_circuits_without_extra_checks(self):
        counts = []

        @commands.refusal_message('Add the invoice attachment.')
        def first(instance, **kwargs):
            counts.append('first')
            return False

        later = Mock(return_value=False)

        class Child(Process):
            refusal_messages = {RefusalReason.CONDITION: 'Child requirements.'}
            transitions = [Transition('go', sources=['draft'], conditions=[first, later],
                                      refusal_messages={RefusalReason.CONDITION: 'Transition requirements.'})]

        class Workflow(Process):
            refusal_messages = {RefusalReason.CONDITION: 'Root requirements.'}
            nested_processes = [Child]

        error = self.refused(self.process(Workflow).go)
        self.assertEqual(error.user_message, "Action 'go' is not allowed: Add the invoice attachment.")
        self.assertEqual(counts, ['first', 'first'])
        later.assert_not_called()

    def test_annotated_process_condition_and_permission_supply_detail(self):
        condition = commands.refusal_message('Enable this workflow.')(deny)
        permission = commands.refusal_message('Ask the invoice owner.')(deny_permission)
        self.addCleanup(lambda: delattr(deny, '_django_logic_refusal_message'))
        self.addCleanup(lambda: delattr(deny_permission, '_django_logic_refusal_message'))

        class ConditionWorkflow(Process):
            conditions = [condition]
            transitions = [Transition('go', sources=['draft'])]

        class PermissionWorkflow(Process):
            permissions = [permission]
            transitions = [Transition('go', sources=['draft'])]

        self.assertEqual(self.refused(self.process(ConditionWorkflow).go).user_message,
                         "Action 'go' is not allowed: Enable this workflow.")
        self.assertEqual(self.refused(lambda: self.process(PermissionWorkflow).go(user=object())).user_message,
                         "Action 'go' is not allowed: Ask the invoice owner.")

    def test_unselected_transition_annotation_cannot_supply_the_message(self):
        @commands.refusal_message('Wrong branch.')
        def wrong_source(instance, **kwargs):
            self.fail('The wrong-source condition must not run.')

        @commands.refusal_message('Complete the current-state requirements.')
        def current_source(instance, **kwargs):
            return False

        class Workflow(Process):
            transitions = [Transition('go', sources=['approved'], conditions=[wrong_source]),
                           Transition('go', sources=['draft'], conditions=[current_source])]

        error = self.refused(self.process(Workflow).go)
        self.assertEqual(error.reason, RefusalReason.CONDITION)
        self.assertEqual(error.user_message, "Action 'go' is not allowed: Complete the current-state requirements.")

    def test_hint_evaluation_cannot_replace_the_selected_detail(self):
        calls = []

        @commands.refusal_message('Original refusal.')
        def condition(instance, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                commands.refusal_message('Hint-only text.')(condition)
            return False

        class Workflow(Process):
            transitions = [Transition('go', sources=['draft'], conditions=[condition])]

        self.assertEqual(self.refused(self.process(Workflow).go).user_message,
                         "Action 'go' is not allowed: Original refusal.")
        self.assertEqual(len(calls), 2)

    def test_custom_validity_cannot_use_an_unrelated_checks_message(self):
        @commands.refusal_message('Unrelated check.')
        def other_condition(instance, **kwargs):
            return False

        other = Transition('other', sources=['draft'], conditions=[other_condition])

        class CustomTransition(Transition):
            def is_valid(self, instance, user=None):
                other.is_valid(instance, user)
                return False

        class Workflow(Process):
            transitions = [CustomTransition('go', sources=['draft'])]

        error = self.refused(self.process(Workflow).go)
        self.assertIsNone(error.reason)
        self.assertEqual(error.user_message, "Action 'go' is not allowed")

    def test_custom_validity_that_delegates_keeps_the_stock_check_message(self):
        @commands.refusal_message('Add a document.')
        def condition(instance, **kwargs):
            return False

        class UserRequired(Transition):
            def is_valid(self, instance, user=None):
                return user is not None and super().is_valid(instance, user)

        class Workflow(Process):
            transitions = [UserRequired('go', sources=['draft'], conditions=[condition])]

        self.assertEqual(self.refused(self.process(Workflow).go).user_message, "Action 'go' is not allowed")
        self.assertEqual(self.refused(lambda: self.process(Workflow).go(user=object())).user_message,
                         "Action 'go' is not allowed: Add a document.")

    def test_strict_permission_failure_does_not_claim_an_unrun_callable(self):
        permission = Mock(return_value=True)
        commands.refusal_message('This callable did not run.')(permission)

        class Workflow(Process):
            permissions_class = StrictPermissions
            permissions = [permission]
            transitions = [Transition('go', sources=['draft'])]

        error = self.refused(self.process(Workflow).go)
        self.assertEqual(error.user_message,
                         "Action 'go' is not allowed: you do not have permission to perform this action.")
        permission.assert_not_called()

    def test_runtime_lock_and_persisted_source_use_resolved_transition_messages(self):
        class Workflow(Process):
            transitions = [Transition('go', sources=['draft'], refusal_messages={
                RefusalReason.LOCKED: 'This invoice is being updated.',
                RefusalReason.SOURCE_STATE: 'The invoice already changed.',
            })]

        process = self.process(Workflow)
        self.assertTrue(process.state.lock())
        try:
            error = self.refused(process.go)
            self.assertEqual(error.user_message, "Action 'go' is not allowed: This invoice is being updated.")
            self.assertEqual(str(error), 'State is locked')
            self.assertIs(type(error), TransitionNotAllowed)
        finally:
            process.state.unlock()
        Invoice.objects.filter(pk=self.invoice.pk).update(status='approved')
        error = self.refused(process.go)
        self.assertEqual(error.user_message, "Action 'go' is not allowed: The invoice already changed.")
        self.assertEqual(error.reason, RefusalReason.SOURCE_STATE)

    def test_runtime_nested_refusal_keeps_inner_action_and_message(self):
        class Inner(Process):
            transitions = [Transition('stop', sources=['draft'], conditions=[deny])]

        def nested(instance, **kwargs):
            self.process(Inner).stop()

        class Outer(Process):
            refusal_messages = {RefusalReason.CONDITION: 'Outer message must not replace the inner refusal.'}
            transitions = [Transition('go', sources=['draft'], side_effects=[nested])]

        error = self.refused(self.process(Outer).go)
        self.assertEqual(error.user_message,
                         "Action 'stop' is not allowed: this record does not meet the requirements for this action.")

    def test_preexisting_user_message_and_technical_exception_are_preserved(self):
        original = TransitionNotAllowed('operator diagnostics', reason=RefusalReason.CONDITION,
                                        user_message='A complete message already supplied.')

        class CustomTransition(Transition):
            def change_state(self, state, **kwargs):
                raise original

        class Workflow(Process):
            transitions = [CustomTransition('go', sources=['draft'])]

        self.assertIs(self.refused(self.process(Workflow).go), original)
        self.assertEqual(original.user_message, 'A complete message already supplied.')
        self.assertEqual(original.args, ('operator diagnostics',))

        technical = ValueError('database detail')

        def broken(instance, **kwargs):
            raise technical

        class BrokenWorkflow(Process):
            transitions = [Transition('go', sources=['draft'], conditions=[broken])]

        with self.assertRaises(ValueError) as caught:
            self.process(BrokenWorkflow).go()
        self.assertIs(caught.exception, technical)
        self.assertFalse(hasattr(technical, 'user_message'))

    def test_raw_transition_calls_keep_user_message_unset(self):
        process = self.process(Process)
        self.assertTrue(process.state.lock())
        self.addCleanup(process.state.unlock)
        error = self.refused(lambda: Transition('go', sources=['draft']).change_state(process.state))
        self.assertIsNone(error.user_message)
        self.assertEqual(str(error), 'State is locked')

    @override_settings(DJANGO_LOGIC=dl_settings(TRANSITION_MESSAGE_RETRY_MINUTES=2))
    def test_background_runtime_refusals_keep_types_and_default_wording(self):
        class Workflow(Process):
            transitions = [Transition('go', sources=['draft']), BackgroundTransition('work', sources=['draft'])]

        process = self.process(Workflow)
        pending = TransitionMessage.objects.create(
            **TransitionMessage.instance_key(self.invoice, process.process_name),
            field_name='status', transition_name='work', kwargs={},
        )
        error = self.refused(process.go)
        self.assertIs(type(error), TransitionTemporarilyUnavailable)
        self.assertEqual(error.user_message,
                         "Action 'go' is temporarily unavailable: a background transition is in progress. Try again shortly.")
        error = self.refused(process.work)
        self.assertIs(type(error), AlreadyInProgress)
        self.assertEqual(error.user_message,
                         "Action 'work' is temporarily unavailable: a background transition is in progress. Try again shortly.")
        TransitionMessage.objects.filter(pk=pending.pk).update(
            modified=timezone.now() - timedelta(minutes=conf.retry_window_minutes() + 1))
        error = self.refused(process.go)
        self.assertIs(type(error), TransitionNotAllowed)
        self.assertEqual(error.user_message,
                         "Action 'go' is not allowed: a background transition for this record is stranded. Please contact support.")


class MessageCallableTests(SimpleTestCase):
    def test_decorator_returns_the_same_callable_and_preserves_its_signature(self):
        def condition(instance, *, extra=None):
            return instance is not None

        signature = inspect.signature(condition)
        decorated = commands.refusal_message('Detail.')(condition)
        self.assertIs(decorated, condition)
        self.assertEqual(inspect.signature(decorated), signature)
        self.assertTrue(decorated(object()))
        self.assertFalse(decorated(None))

    def test_shared_condition_bundle_keeps_concurrent_messages_separate(self):
        barrier = Barrier(2, timeout=5)

        @commands.refusal_message('First requirement.')
        def first(instance, **kwargs):
            barrier.wait()
            return not instance.is_available

        @commands.refusal_message('Second requirement.')
        def second(instance, **kwargs):
            return False

        class Workflow(Process):
            transitions = [Transition('go', sources=['draft'], conditions=[first, second])]

        def refuse(flag):
            instance = Invoice(pk=123, status='draft', is_available=flag)
            process = Workflow(instance=instance, field_name='status')
            try:
                process.go()
            except TransitionNotAllowed as error:
                return error.user_message
            self.fail('The action must be refused.')

        with ThreadPoolExecutor(max_workers=2) as executor:
            messages = list(executor.map(refuse, [True, False]))
        self.assertEqual(messages, ["Action 'go' is not allowed: First requirement.",
                                    "Action 'go' is not allowed: Second requirement."])
