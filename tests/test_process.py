from django.test import TestCase

from demo.models import Invoice

from django_logic import Process, Transition
from django_logic.exceptions import TransitionNotAllowed


class User:
    def __init__(self, is_allowed=True, is_staff=False):
        self.is_allowed = is_allowed
        self.is_staff = is_staff


def allowed(instance, user):
    return user.is_allowed and not user.is_staff


def is_staff(instance, user):
    return user.is_staff


def disallow(instance, user):
    return False


def is_editable(instance):
    return not instance.customer_received


def is_available(instance):
    return instance.is_available


def not_available(instance):
    return False


def disable_invoice(invoice: Invoice, *args, **kwargs):
    invoice.is_available = False
    invoice.customer_received = False
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


class ValidateProcessTestCase(TestCase):
    def setUp(self) -> None:
        self.user = User()

    def test_pure_process(self):
        class MyProcess(Process):
            pass

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertTrue(process.is_valid())
        self.assertTrue(process.is_valid(self.user))

    def test_empty_permissions(self):
        class MyProcess(Process):
            permissions = []

        self.assertTrue(MyProcess('state', instance=Invoice(status='draft')).is_valid())
        self.assertTrue(MyProcess('state', instance=Invoice(status='draft')).is_valid(self.user))

    def test_permissions_successfully(self):
        class MyProcess(Process):
            permissions = [allowed]

        self.assertTrue(MyProcess('state', instance=Invoice(status='draft')).is_valid())
        self.assertTrue(MyProcess('state', instance=Invoice(status='draft')).is_valid(self.user))

    def test_permission_fail(self):
        self.user.is_allowed = False

        class MyProcess(Process):
            permissions = [allowed]

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertTrue(process.is_valid())
        self.assertFalse(process.is_valid(self.user))

        class AnotherProcess(Process):
            permissions = [allowed, disallow]

        process = AnotherProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertTrue(process.is_valid())
        self.assertFalse(process.is_valid(self.user))

    def test_empty_conditions(self):
        class MyProcess(Process):
            conditions = []

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertTrue(process.is_valid(self.user))

    def test_conditions_successfully(self):
        class MyProcess(Process):
            conditions = [is_editable]

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertTrue(process.is_valid())
        self.assertTrue(process.is_valid(self.user))

    def test_conditions_fail(self):
        class MyProcess(Process):
            conditions = [not_available]

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))

        self.assertFalse(process.is_valid())
        self.assertFalse(process.is_valid(self.user))

        class AnotherProcess(Process):
            conditions = [is_editable]

        instance = Invoice(status='draft')
        instance.customer_received = True
        process = AnotherProcess(field_name='status', instance=instance)
        self.assertFalse(process.is_valid())
        self.assertFalse(process.is_valid(self.user))

    def test_permissions_and_conditions_successfully(self):
        class MyProcess(Process):
            permissions = [allowed]
            conditions = [is_editable]

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertTrue(process.is_valid())
        self.assertTrue(process.is_valid(self.user))

    def test_permissions_and_conditions_fail(self):
        class MyProcess(Process):
            permissions = [allowed, disallow]
            conditions = [is_editable]

        process = MyProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertTrue(process.is_valid())
        self.assertFalse(process.is_valid(self.user))

        class AnotherProcess(Process):
            permissions = [allowed]
            conditions = [is_editable, not_available]

        process = AnotherProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertFalse(process.is_valid())
        self.assertFalse(process.is_valid(self.user))

        class FinalProcess(Process):
            permissions = [allowed, disallow]
            conditions = [is_editable, not_available]

        process = FinalProcess(field_name='status', instance=Invoice(status='draft'))
        self.assertFalse(process.is_valid())
        self.assertFalse(process.is_valid(self.user))

    def test_getattr_is_valid_name_and_transition(self):
        class MyProcess(Process):
            transitions = [Transition('is_valid', sources=['draft'], target='valid')]

        invoice = Invoice.objects.create(status='draft')
        process = MyProcess(instance=invoice, field_name='status')
        # transition shouldn't be executed
        self.assertTrue(process.is_valid())
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'draft')


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
            permissions = [allowed]
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
            permissions = [allowed]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            permissions = [allowed]
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            permissions = [allowed]
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
            permissions = [allowed]
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        class GrandParentProcess(Process):
            permissions = [allowed]
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
            permissions = [allowed]
            conditions = [is_editable]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            permissions = [allowed]
            conditions = [is_editable]
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            permissions = [allowed]
            conditions = [is_editable]
            nested_processes = (ParentProcess,)

        process = GrandParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

    def test_nested_process_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            permissions = [allowed]
            conditions = [is_editable]
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            permissions = [allowed]
            conditions = [is_editable]
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Invoice.objects.create(status='draft'), field_name='status')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            permissions = [allowed]
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
            permissions = [allowed]
            conditions = [is_editable]
            transitions = [transition1, transition2]

        class ParentProcess(Process):
            permissions = [allowed]
            conditions = [is_editable]
            nested_processes = (ChildProcess,)
            transitions = [transition3, transition4]

        class GrandParentProcess(Process):
            permissions = [allowed]
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
            permissions = [allowed]
            conditions = [is_editable]
            nested_processes = (ChildProcess,)
            transitions = [transition3, transition4]

        class GrandParentProcess(Process):
            permissions = [allowed]
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
        transition1 = Transition('action', permissions=[allowed], conditions=[is_editable],
                                 sources=['draft'],
                                 target='done')
        transition2 = Transition('action', permissions=[allowed], conditions=[is_editable],
                                 sources=['done'],
                                 target='closed')
        transition3 = Transition('action',
                                 permissions=[allowed],
                                 conditions=[is_editable],
                                 sources=['draft'],
                                 target='approved')
        transition4 = Transition('action',
                                 permissions=[allowed],
                                 conditions=[is_editable],
                                 sources=['done'],
                                 target='closed')
        transition5 = Transition('action',
                                 permissions=[allowed],
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
                                 permissions=[allowed],
                                 conditions=[is_editable],
                                 sources=['draft'],
                                 target='done')
        transition2 = Transition('action',
                                 permissions=[disallow],
                                 conditions=[is_editable],
                                 sources=['done'],
                                 target='closed')
        transition3 = Transition('action',
                                 permissions=[allowed],
                                 conditions=[not_available],
                                 sources=['draft'],
                                 target='approved')
        transition4 = Transition('action',
                                 permissions=[allowed],
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

    def test_getattr_get_available_transition_name_and_transition(self):
        class MyProcess(Process):
            transitions = [Transition('get_available_transition', sources=['draft'], target='valid')]

        invoice = Invoice.objects.create(status='draft')
        process = MyProcess(instance=invoice, field_name='status')
        # transition shouldn't be executed
        self.assertEqual(list(process.get_available_transitions()), MyProcess.transitions)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'draft')

    def test_get_non_existing_transition(self):
        class MyProcess(Process):
            transitions = [Transition('validate', sources=['draft'], target='valid')]

        invoice = Invoice.objects.create(status='draft')
        process = MyProcess(instance=invoice, field_name='status')
        with self.assertRaises(TransitionNotAllowed):
            process.test()


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

        TestProcess(instance=self.invoice, field_name='status').cancel()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'cancelled')
        TestProcess(instance=self.invoice, field_name='status').undo()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'draft')

    def test_transition_with_side_effect(self):
        class TestProcess(Process):
            transitions = [
                Transition('cancel', sources=['draft', ], target='cancelled', side_effects=[disable_invoice]),
                Transition('undo', sources=['cancelled'], target='draft', side_effects=[update_invoice])
            ]
        self.assertTrue(self.invoice.is_available)
        TestProcess(instance=self.invoice, field_name='status').cancel()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'cancelled')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        TestProcess(instance=self.invoice, field_name='status').undo(is_available=True, customer_received=True)
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
        TestProcess(instance=self.invoice, field_name='status').cancel()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'cancelled')
        self.assertFalse(self.invoice.is_available)
        self.assertFalse(self.invoice.customer_received)
        TestProcess(instance=self.invoice, field_name='status').undo(is_available=True, customer_received=True)
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

    def test_same_action_name_with_several_conditions(self):
        class TestProcess(Process):
            transitions = [
                Transition('cancel', sources=['draft', ], target='cancelled', conditions=[is_editable]),
                Transition('cancel', sources=['draft', ], target='cancelled', conditions=[is_available])
            ]
        invoice = Invoice.objects.create(status='draft')
        update_invoice(invoice, customer_received=True, is_available=False)

        with self.assertRaises(TransitionNotAllowed):
            # not editable and not available
            TestProcess('status', invoice).cancel()
        self.assertEqual(invoice.status, 'draft')

        # is editable, but not available
        update_invoice(invoice, customer_received=False, is_available=False)
        TestProcess('status', invoice).cancel()
        self.assertEqual(invoice.status, 'cancelled')

        invoice = Invoice.objects.create(status='draft')
        # is available, but not editable
        update_invoice(invoice, customer_received=True, is_available=True)
        TestProcess('status', invoice).cancel()
        self.assertEqual(invoice.status, 'cancelled')

    def test_same_action_name_with_several_permissions(self):
        class TestProcess(Process):
            transitions = [
                Transition('cancel', sources=['draft', ], target='cancelled', permissions=[is_staff]),
                Transition('cancel', sources=['draft', ], target='cancelled', permissions=[allowed])
            ]

        user = User(is_allowed=True)
        staff = User(is_staff=True)

        invoice = Invoice.objects.create(status='draft')
        with self.assertRaises(TransitionNotAllowed):
            # either user or staff
            TestProcess('status', invoice).cancel()

        self.assertEqual(invoice.status, 'draft')
        TestProcess('status', invoice).cancel(user=user)
        self.assertEqual(invoice.status, 'cancelled')

        invoice = Invoice.objects.create(status='draft')
        TestProcess('status', invoice).cancel(user=staff)
        self.assertEqual(invoice.status, 'cancelled')
