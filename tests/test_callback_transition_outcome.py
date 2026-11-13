"""A failed optional follow-up must not change the successful action's result."""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TransactionTestCase

from django_logic import Process, Transition
from django_logic.process import ProcessManager
from tests.models import Invoice


CALLBACKS = []


def record_approval(instance, **kwargs):
    CALLBACKS.append(instance.pk)


def missing_related_record(instance, **kwargs):
    raise LookupError('The follow-up needs a missing related record.')


def missing_permission_record(instance, user, **kwargs):
    raise LookupError('The follow-up needs a missing permission record.')


def invalid_database_check(instance, **kwargs):
    Invoice.objects.create(status=None)


def check_related_after_approval(instance, **kwargs):
    instance.followup_outcome.check_related(**kwargs)


def check_permission_after_approval(instance, **kwargs):
    instance.followup_outcome.check_permission(**kwargs)


def check_database_after_approval(instance, **kwargs):
    instance.followup_outcome.check_database(**kwargs)


class FollowupOutcomeProcess(Process):
    process_name = 'followup_outcome'
    transitions = [
        Transition('approve', sources=['draft'], target='approved',
                   callbacks=[record_approval, check_related_after_approval]),
        Transition('approve_with_user', sources=['draft'], target='approved',
                   callbacks=[record_approval, check_permission_after_approval]),
        Transition('approve_with_database_check', sources=['draft'], target='approved',
                   callbacks=[record_approval, check_database_after_approval]),
        Transition('check_related', sources=['approved'], target='done',
                   conditions=[missing_related_record]),
        Transition('check_permission', sources=['approved'], target='done',
                   permissions=[missing_permission_record]),
        Transition('check_database', sources=['approved'], target='done',
                   conditions=[invalid_database_check]),
    ]


class CallbackTransitionOutcomeTests(TransactionTestCase):
    def setUp(self):
        CALLBACKS.clear()
        ProcessManager.bind_model_process(
            Invoice, FollowupOutcomeProcess, state_field='status')
        self.addCleanup(self.remove_binding)
        self.invoice = Invoice.objects.create(status='draft')

    @staticmethod
    def remove_binding():
        ProcessManager.bindings = [
            binding for binding in ProcessManager.bindings
            if binding.process_class is not FollowupOutcomeProcess
        ]
        delattr(Invoice, 'followup_outcome')

    def assert_approval_completed(self):
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'approved')
        self.assertEqual(CALLBACKS, [self.invoice.pk])

    def test_raising_followup_condition_does_not_report_approval_as_failed(self):
        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            self.invoice.followup_outcome.approve()

        self.assert_approval_completed()
        self.assertIn('check_related_after_approval', '\n'.join(logs.output))
        self.assertIn('missing related record', '\n'.join(logs.output))

    def test_raising_followup_permission_does_not_report_approval_as_failed(self):
        user = get_user_model().objects.create(username='followup-reviewer')
        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            self.invoice.followup_outcome.approve_with_user(user=user)

        self.assert_approval_completed()
        self.assertIn('check_permission_after_approval', '\n'.join(logs.output))
        self.assertIn('missing permission record', '\n'.join(logs.output))

    def test_database_guard_failure_preserves_approval_and_caller_transaction(self):
        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            with transaction.atomic():
                self.invoice.followup_outcome.approve_with_database_check()
                saved_after_failure = Invoice.objects.create(status='healthy')

        self.assert_approval_completed()
        self.assertTrue(Invoice.objects.filter(pk=saved_after_failure.pk).exists())
        self.assertIn('check_database_after_approval', '\n'.join(logs.output))
