from django.test import TestCase

from demo.models import Invoice

from django_logic import Process, Transition


class User:
    is_allowed = True


def allow(instance, user):
    return user is not None and user.is_allowed


def disallow(instance, user):
    return False


def is_editable(instance):
    return not instance.customer_received


def not_available(instance):
    return False


class ValidateProcessTestCase(TestCase):
    def setUp(self) -> None:
        self.user = User()

    def test_pure_process(self):
        class MyProcess(Process):
            pass

        process = MyProcess(field_name='status')
        self.assertTrue(process.validate())
        self.assertTrue(process.validate(self.user))

    def test_empty_permissions(self):
        class MyProcess(Process):
            permissions = []

        self.assertTrue(MyProcess('state').validate())
        self.assertTrue(MyProcess('state').validate(self.user))

    def test_permissions_successfully(self):
        class MyProcess(Process):
            permissions = [allow]

        self.assertFalse(MyProcess('state').validate())
        self.assertTrue(MyProcess('state').validate(self.user))

    def test_permission_fail(self):
        self.user.is_allowed = False

        class MyProcess(Process):
            permissions = [allow]

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

        class AnotherProcess(Process):
            permissions = [allow, disallow]

        process = AnotherProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

    def test_empty_conditions(self):
        class MyProcess(Process):
            conditions = []

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertTrue(process.validate(self.user))

    def test_conditions_successfully(self):
        class MyProcess(Process):
            conditions = [is_editable]

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertTrue(process.validate())
        self.assertTrue(process.validate(self.user))

    def test_conditions_fail(self):
        class MyProcess(Process):
            conditions = [not_available]

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))

        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

        class AnotherProcess(Process):
            conditions = [is_editable]

        instance = Invoice(status='draft')
        instance.customer_received = True
        process = AnotherProcess(field_name='status', instance=instance)
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

    def test_permissions_and_conditions_successfully(self):
        class MyProcess(Process):
            permissions = [allow]
            conditions = [is_editable]

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertFalse(process.validate())
        self.assertTrue(process.validate(self.user))

    def test_permissions_and_conditions_fail(self):
        class MyProcess(Process):
            permissions = [allow, disallow]
            conditions = [is_editable]

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

        class AnotherProcess(Process):
            permissions = [allow]
            conditions = [is_editable, not_available]

        process = AnotherProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

        class FinalProcess(Process):
            permissions = [allow, disallow]
            conditions = [is_editable, not_available]

        process = FinalProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))


class GetAvailableTransitionsTestCase(TestCase):
    def setUp(self) -> None:
        self.user = User()

    def test_pure_process(self):
        class ChildProcess(Process):
            pass

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions()), [])

    def test_process(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions()), [transition1])

        process = ChildProcess(instance=Invoice.objects.create(status='done'), field_name='status')
        self.assertEqual(list(process.get_available_transitions()), [transition2])

        process = ChildProcess(instance=Invoice.objects.create(status='closed'), field_name='status')
        self.assertEqual(list(process.get_available_transitions()), [])

    def test_process_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            conditions = [not_available]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions()), [])

        process = ChildProcess(instance=Invoice.objects.create(status='done'), field_name='status')
        self.assertEqual(list(process.get_available_transitions()), [])

        process = ChildProcess(instance=Invoice.objects.create(status='closed'), field_name='status')
        self.assertEqual(list(process.get_available_transitions()), [])

    def test_conditions_and_permissions_successfully(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            conditions = [is_editable]
            permissions = [allow]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        process = ChildProcess(instance=Invoice.objects.create(status='done'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition2])

        process = ChildProcess(instance=Invoice.objects.create(status='closed'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

    def test_conditions_and_permissions_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            conditions = [is_editable]
            permissions = [disallow]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        process = ChildProcess(instance=Invoice.objects.create(status='done'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        process = ChildProcess(instance=Invoice.objects.create(status='closed'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

    def test_nested_process_permissions_successfully(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            permissions = [allow]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            permissions = [allow]
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            permissions = [allow]
            nested_processes = (ParentProcess,)

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

    def test_nested_process_permissions_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            permissions = [disallow]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        class ParentProcess(Process):
            permissions = [allow]
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        class GrandParentProcess(Process):
            permissions = [allow]
            nested_processes = (ParentProcess,)

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

    def test_nested_process_conditions_successfully(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            conditions = [is_editable]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            conditions = [is_editable]
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            conditions = [is_editable]
            nested_processes = (ParentProcess,)

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

    def test_nested_process_conditions_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            conditions = [is_editable]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            conditions = [not_available]
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        class GrandParentProcess(Process):
            conditions = [is_editable]
            nested_processes = (ParentProcess,)

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

    def test_nested_process_successfully(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            permissions = [allow]
            conditions = [is_editable]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            permissions = [allow]
            conditions = [is_editable]
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            permissions = [allow]
            conditions = [is_editable]
            nested_processes = (ParentProcess,)

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

    def test_nested_process_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            permissions = [allow]
            conditions = [is_editable]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            permissions = [allow]
            conditions = [is_editable]
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            permissions = [allow]
            conditions = [not_available]
            nested_processes = (ParentProcess,)

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

    def test_nested_process_with_nested_transitions_successfully(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')
        transition3 = Transition('action', sources=['draft'], target='approved')
        transition4 = Transition('action', sources=['done'], target='closed')
        transition5 = Transition('action', sources=['draft'], target='declined')

        class ChildProcess(Process):
            permissions = [allow]
            conditions = [is_editable]
            transitions = [transition1, transition2]

        class ParentProcess(Process):
            permissions = [allow]
            conditions = [is_editable]
            nested_processes = (ChildProcess,)
            transitions = [transition3, transition4]

        class GrandParentProcess(Process):
            permissions = [allow]
            conditions = [is_editable]
            nested_processes = (ParentProcess,)

            transitions = [transition5]

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')

        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition1, transition3, transition5])

        process = GrandParentProcess(instance=Invoice.objects.create(status='done'), field_name='status')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition2, transition4])

    def test_nested_process_with_nested_transitions_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')
        transition3 = Transition('action', sources=['draft'], target='approved')
        transition4 = Transition('action', sources=['done'], target='closed')
        transition5 = Transition('action', sources=['draft'], target='declined')

        class ChildProcess(Process):
            permissions = [disallow]
            conditions = [is_editable]
            transitions = [transition1, transition2]

        class ParentProcess(Process):
            permissions = [allow]
            conditions = [is_editable]
            nested_processes = (ChildProcess,)
            transitions = [transition3, transition4]

        class GrandParentProcess(Process):
            permissions = [allow]
            conditions = [is_editable]
            nested_processes = (ParentProcess,)

            transitions = [transition5]

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition3, transition5])

        process = GrandParentProcess(instance=Invoice.objects.create(status='done'), field_name='status')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition4])

    def test_nested_process_with_nested_transitions_conditions_and_permissions_successfully(self):
        transition1 = Transition('action', permissions=[allow], conditions=[is_editable],
                                 sources=['draft'],
                                 target='done')
        transition2 = Transition('action', permissions=[allow], conditions=[is_editable],
                                 sources=['done'],
                                 target='closed')
        transition3 = Transition('action',
                                 permissions=[allow],
                                 conditions=[is_editable],
                                 sources=['draft'],
                                 target='approved')
        transition4 = Transition('action',
                                 permissions=[allow],
                                 conditions=[is_editable],
                                 sources=['done'],
                                 target='closed')
        transition5 = Transition('action',
                                 permissions=[allow],
                                 conditions=[is_editable],
                                 sources=['draft'],
                                 target='declined')

        class ChildProcess(Process):
            transitions = [transition1, transition2]

        class ParentProcess(Process):
            nested_processes = (ChildProcess,)
            transitions = [transition3, transition4]

        class GrandParentProcess(Process):
            nested_processes = (ParentProcess,)

            transitions = [transition5]

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition1, transition3, transition5])

        process = GrandParentProcess(instance=Invoice.objects.create(status='done'), field_name='status')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition2, transition4])

    def test_nested_process_with_nested_transitions_conditions_and_permissions_fail(self):
        transition1 = Transition('action',
                                 permissions=[allow],
                                 conditions=[is_editable],
                                 sources=['draft'],
                                 target='done')
        transition2 = Transition('action',
                                 permissions=[disallow],
                                 conditions=[is_editable],
                                 sources=['done'],
                                 target='closed')
        transition3 = Transition('action',
                                 permissions=[allow],
                                 conditions=[not_available],
                                 sources=['draft'],
                                 target='approved')
        transition4 = Transition('action',
                                 permissions=[allow],
                                 conditions=[is_editable],
                                 sources=['done'],
                                 target='closed')
        transition5 = Transition('action',
                                 permissions=[disallow],
                                 conditions=[not_available],
                                 sources=['draft'],
                                 target='declined')

        class ChildProcess(Process):
            transitions = [transition1, transition2]

        class ParentProcess(Process):
            nested_processes = (ChildProcess,)
            transitions = [transition3, transition4]

        class GrandParentProcess(Process):
            nested_processes = (ParentProcess,)

            transitions = [transition5]

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition1])

        process = GrandParentProcess(instance=Invoice.objects.create(status='done'), field_name='status')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition4])


def disable_invoice(invoice: Invoice):
    invoice.is_available = False
    invoice.customer_received = False
    invoice.save()


def update_invoice(invoice, is_available, customer_received):
    invoice.is_available = is_available
    invoice.customer_received = customer_received
    invoice.save()


def enable_invoice(invoice: Invoice):
    invoice.is_available = True
    invoice.save()


def fail_invoice(invoice: Invoice):
    raise Exception


class ApplyTransitionTestCase(TestCase):
    def setUp(self) -> None:
        self.user = User()
        self.invoice = Invoice.objects.create(status='draft')

    def test_simple_transition(self):
        class TestProcess(Process):
            transitions = [
                Transition('cancel', sources=['draft', ], target='cancelled')
            ]

        process = TestProcess(instance=self.invoice, field_name='status')
        process.cancel()
        self.assertEqual(self.invoice.status, 'cancelled')

    def test_several_transitions(self):
        class TestProcess(Process):
            transitions = [
                Transition('cancel', sources=['draft', ], target='cancelled'),
                Transition('undo', sources=['cancelled'], target='draft')
            ]

        process = TestProcess(instance=self.invoice, field_name='status')
        process.cancel()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'cancelled')
        process.undo()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')

    def test_transition_with_side_effect(self):
        class TestProcess(Process):
            transitions = [
                Transition('cancel', sources=['draft', ], target='cancelled', side_effects=[disable_invoice]),
                Transition('undo', sources=['cancelled'], target='draft', side_effects=[update_invoice])
            ]
        self.assertTrue(self.invoice.is_available)
        process = TestProcess(instance=self.invoice, field_name='status')
        process.cancel()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'cancelled')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        process.undo(is_available=True, customer_received=True)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)

    def test_transition_with_callbacks(self):
        class TestProcess(Process):
            transitions = [
                Transition('cancel', sources=['draft', ], target='cancelled', callbacks=[disable_invoice]),
                Transition('undo', sources=['cancelled'], target='draft', callbacks=[update_invoice])
            ]
        self.assertTrue(self.invoice.is_available)
        process = TestProcess(instance=self.invoice, field_name='status')
        process.cancel()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'cancelled')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        process.undo(is_available=True, customer_received=True)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)

    def test_transition_with_failure_callbacks(self):
        class TestProcess(Process):
            transitions = [
                Transition('cancel', sources=['draft', ], target='cancelled', callbacks=[disable_invoice]),
                Transition('undo', sources=['draft'], target='draft', side_effects=[fail_invoice], failed_state='failed', failure_callbacks=[update_invoice])
            ]
        update_invoice(self.invoice, is_available=False, customer_received=False)
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        self.assertEqual(self.invoice.status, 'draft')
        process = TestProcess(instance=self.invoice, field_name='status')
        process.undo(is_available=True, customer_received=True)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'failed')
        self.assertTrue(self.invoice.is_available)
        self.assertTrue(self.invoice.customer_received)
