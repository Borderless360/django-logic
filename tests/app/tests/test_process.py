from django.test import TestCase

from app.models import Invoice
from app.process import InvoiceProcess

from django_logic import Process, Transition, Permissions, Conditions


class InvoiceProcessTestCase(TestCase):
    def setUp(self):
        self.process_class = InvoiceProcess

    def test_process_class_method(self):
        self.assertEqual(self.process_class.get_process_name(), 'Invoice Process')

    def test_invoice_process(self):
        # TODO: remove once everything else is covered
        invoice = Invoice.objects.create(status='draft')
        invoice.status_process.approve()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'approved')


class User:
    is_allowed = True


class Instance:
    def __init__(self, state):
        self.is_available = True
        self.state = state


def allow(instance, user):
    return user is not None and user.is_allowed


def disallow(instance, user):
    return False


def is_available(instance):
    return instance.is_available


def not_available(instance):
    return False


class ValidateProcessTestCase(TestCase):
    def setUp(self) -> None:
        self.user = User()

    def test_pure_process(self):
        class MyProcess(Process):
            pass

        process = MyProcess(state_field='state')
        self.assertTrue(process.validate())
        self.assertTrue(process.validate(self.user))

    def test_empty_permissions(self):
        class MyProcess(Process):
            permissions = Permissions([])

        self.assertTrue(MyProcess('state').validate())
        self.assertTrue(MyProcess('state').validate(self.user))

    def test_permissions_successfully(self):
        class MyProcess(Process):
            permissions = Permissions([allow])

        self.assertFalse(MyProcess('state').validate())
        self.assertTrue(MyProcess('state').validate(self.user))

    def test_permission_fail(self):
        self.user.is_allowed = False

        class MyProcess(Process):
            permissions = Permissions([allow])

        process = MyProcess(state_field='state', instance=Instance('draft'))
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

        class AnotherProcess(Process):
            permissions = Permissions([allow, disallow])

        process = AnotherProcess(state_field='state', instance=Instance('draft'))
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

    def test_empty_conditions(self):
        class MyProcess(Process):
            conditions = Conditions([])

        process = MyProcess(state_field='state', instance=Instance('draft'))
        self.assertTrue(process.validate(self.user))

    def test_conditions_successfully(self):
        class MyProcess(Process):
            conditions = Conditions([is_available])

        process = MyProcess(state_field='state', instance=Instance('draft'))
        self.assertTrue(process.validate())
        self.assertTrue(process.validate(self.user))

    def test_conditions_fail(self):
        class MyProcess(Process):
            conditions = Conditions([not_available])

        process = MyProcess(state_field='state', instance=Instance('draft'))

        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

        class AnotherProcess(Process):
            conditions = Conditions([is_available])

        instance = Instance('draft')
        instance.is_available = False
        process = AnotherProcess(state_field='state', instance=instance)
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

    def test_permissions_and_conditions_successfully(self):
        class MyProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])

        process = MyProcess(state_field='state', instance=Instance('draft'))
        self.assertFalse(process.validate())
        self.assertTrue(process.validate(self.user))

    def test_permissions_and_conditions_fail(self):
        class MyProcess(Process):
            permissions = Permissions([allow, disallow])
            conditions = Conditions([is_available])

        process = MyProcess(state_field='state', instance=Instance('draft'))
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

        class AnotherProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available, not_available])

        process = AnotherProcess(state_field='state', instance=Instance('draft'))
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))

        class FinalProcess(Process):
            permissions = Permissions([allow, disallow])
            conditions = Conditions([is_available, not_available])

        process = FinalProcess(state_field='state', instance=Instance('draft'))
        self.assertFalse(process.validate())
        self.assertFalse(process.validate(self.user))


class GetAvailableTransitionsTestCase(TestCase):
    def setUp(self) -> None:
        self.user = User()

    def test_pure_process(self):
        class ChildProcess(Process):
            pass

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions()), [])

    def test_process(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions()), [transition1])

        process = ChildProcess(instance=Instance('done'), state_field='state')
        self.assertEqual(list(process.get_available_transitions()), [transition2])

        process = ChildProcess(instance=Instance('closed'), state_field='state')
        self.assertEqual(list(process.get_available_transitions()), [])

    def test_process_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            conditions = Conditions([not_available])
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions()), [])

        process = ChildProcess(instance=Instance('done'), state_field='state')
        self.assertEqual(list(process.get_available_transitions()), [])

        process = ChildProcess(instance=Instance('closed'), state_field='state')
        self.assertEqual(list(process.get_available_transitions()), [])

    def test_conditions_and_permissions_successfully(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            conditions = Conditions([is_available])
            permissions = Permissions([allow])
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        process = ChildProcess(instance=Instance('done'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition2])

        process = ChildProcess(instance=Instance('closed'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

    def test_conditions_and_permissions_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            conditions = Conditions([is_available])
            permissions = Permissions([disallow])
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        process = ChildProcess(instance=Instance('done'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        process = ChildProcess(instance=Instance('closed'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

    def test_nested_process_permissions_successfully(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            permissions = Permissions([allow])
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            permissions = Permissions([allow])
            nested_processes = (ChildProcess, )

        process = ParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            permissions = Permissions([allow])
            nested_processes = (ParentProcess, )

        process = GrandParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

    def test_nested_process_permissions_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            permissions = Permissions([disallow])
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        class ParentProcess(Process):
            permissions = Permissions([allow])
            nested_processes = (ChildProcess,)

        process = ParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        class GrandParentProcess(Process):
            permissions = Permissions([allow])
            nested_processes = (ParentProcess,)

        process = GrandParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

    def test_nested_process_conditions_successfully(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            conditions = Conditions([is_available])
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            conditions = Conditions([is_available])
            nested_processes = (ChildProcess, )

        process = ParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            conditions = Conditions([is_available])
            nested_processes = (ParentProcess, )

        process = GrandParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

    def test_nested_process_conditions_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            conditions = Conditions([is_available])
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            conditions = Conditions([not_available])
            nested_processes = (ChildProcess, )

        process = ParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

        class GrandParentProcess(Process):
            conditions = Conditions([is_available])
            nested_processes = (ParentProcess, )

        process = GrandParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

    def test_nested_process_successfully(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])
            nested_processes = (ChildProcess, )

        process = ParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])
            nested_processes = (ParentProcess, )

        process = GrandParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

    def test_nested_process_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')

        class ChildProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])
            transitions = [transition1, transition2]

        process = ChildProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class ParentProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])
            nested_processes = (ChildProcess, )

        process = ParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [transition1])

        class GrandParentProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([not_available])
            nested_processes = (ParentProcess, )

        process = GrandParentProcess(instance=Instance('draft'), state_field='state')
        self.assertEqual(list(process.get_available_transitions(self.user)), [])

    def test_nested_process_with_nested_transitions_successfully(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')
        transition3 = Transition('action', sources=['draft'], target='approved')
        transition4 = Transition('action', sources=['done'], target='closed')
        transition5 = Transition('action', sources=['draft'], target='declined')

        class ChildProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])
            transitions = [transition1, transition2]

        class ParentProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])
            nested_processes = (ChildProcess,)
            transitions = [transition3, transition4]

        class GrandParentProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])
            nested_processes = (ParentProcess,)

            transitions = [transition5]

        process = GrandParentProcess(instance=Instance('draft'), state_field='state')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition1, transition3, transition5])

        process = GrandParentProcess(instance=Instance('done'), state_field='state')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition2, transition4])

    def test_nested_process_with_nested_transitions_fail(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['done'], target='closed')
        transition3 = Transition('action', sources=['draft'], target='approved')
        transition4 = Transition('action', sources=['done'], target='closed')
        transition5 = Transition('action', sources=['draft'], target='declined')

        class ChildProcess(Process):
            permissions = Permissions([disallow])
            conditions = Conditions([is_available])
            transitions = [transition1, transition2]

        class ParentProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])
            nested_processes = (ChildProcess,)
            transitions = [transition3, transition4]

        class GrandParentProcess(Process):
            permissions = Permissions([allow])
            conditions = Conditions([is_available])
            nested_processes = (ParentProcess,)

            transitions = [transition5]

        process = GrandParentProcess(instance=Instance('draft'), state_field='state')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition3, transition5])

        process = GrandParentProcess(instance=Instance('done'), state_field='state')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition4])

    def test_nested_process_with_nested_transitions_conditions_and_permissions_successfully(self):
        transition1 = Transition('action',
                                 permissions=Permissions([allow]),
                                 conditions=Conditions([is_available]),
                                 sources=['draft'],
                                 target='done')
        transition2 = Transition('action',
                                 permissions=Permissions([allow]),
                                 conditions=Conditions([is_available]),
                                 sources=['done'],
                                 target='closed')
        transition3 = Transition('action',
                                 permissions=Permissions([allow]),
                                 conditions=Conditions([is_available]),
                                 sources=['draft'],
                                 target='approved')
        transition4 = Transition('action',
                                 permissions=Permissions([allow]),
                                 conditions=Conditions([is_available]),
                                 sources=['done'],
                                 target='closed')
        transition5 = Transition('action',
                                 permissions=Permissions([allow]),
                                 conditions=Conditions([is_available]),
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

        process = GrandParentProcess(instance=Instance('draft'), state_field='state')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition1, transition3, transition5])

        process = GrandParentProcess(instance=Instance('done'), state_field='state')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition2, transition4])

    def test_nested_process_with_nested_transitions_conditions_and_permissions_fail(self):
        transition1 = Transition('action',
                                 permissions=Permissions([allow]),
                                 conditions=Conditions([is_available]),
                                 sources=['draft'],
                                 target='done')
        transition2 = Transition('action',
                                 permissions=Permissions([disallow]),
                                 conditions=Conditions([is_available]),
                                 sources=['done'],
                                 target='closed')
        transition3 = Transition('action',
                                 permissions=Permissions([allow]),
                                 conditions=Conditions([not_available]),
                                 sources=['draft'],
                                 target='approved')
        transition4 = Transition('action',
                                 permissions=Permissions([allow]),
                                 conditions=Conditions([is_available]),
                                 sources=['done'],
                                 target='closed')
        transition5 = Transition('action',
                                 permissions=Permissions([disallow]),
                                 conditions=Conditions([not_available]),
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

        process = GrandParentProcess(instance=Instance('draft'), state_field='state')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition1])

        process = GrandParentProcess(instance=Instance('done'), state_field='state')
        for transition in process.get_available_transitions(self.user):
            self.assertIn(transition, [transition4])
