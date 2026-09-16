"""Branches told apart by who is asking need the opposite of the default.

``Permissions`` reads ``user=None`` as "no user context" and permits. When
one action name is split across nested processes by caller, the person's
branch must refuse a call with no user and the automation branch must
refuse a call with one — or a caller matches both and the engine refuses
the action as ambiguous.
"""
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from django_logic import Process, ProcessManager, Transition
from django_logic.commands import NoUserPermissions, StrictPermissions
from django_logic.exceptions import TransitionNotAllowed
from tests.models import Invoice

_ran = []


def _record_person(instance, **kwargs):
    _ran.append('person')


def _record_automation(instance, **kwargs):
    _ran.append('automation')


def _is_staff(instance, user, **kwargs):
    return user.is_staff


class _Staff:
    is_staff = True


class _Guest:
    is_staff = False


class ClassTests(SimpleTestCase):
    def test_strict_refuses_no_user_then_runs_its_commands(self):
        strict = StrictPermissions(commands=[_is_staff])
        self.assertFalse(strict.execute(None, None))
        self.assertFalse(strict.execute(None, _Guest()))
        self.assertTrue(strict.execute(None, _Staff()))

    def test_no_user_refuses_a_user_and_permits_none(self):
        automation = NoUserPermissions(commands=[])
        self.assertFalse(automation.execute(None, _Staff()))
        self.assertTrue(automation.execute(None, None))


class PersonBranch(Process):
    permissions_class = StrictPermissions
    permissions = [_is_staff]
    transitions = [Transition('close', sources=['draft'], side_effects=[_record_person])]


class AutomationBranch(Process):
    permissions_class = NoUserPermissions
    transitions = [Transition('close', sources=['draft'], side_effects=[_record_automation])]


class BranchedProcess(Process):
    process_name = 'branched'
    nested_processes = [PersonBranch, AutomationBranch]


class BranchTests(TestCase):
    def setUp(self):
        ProcessManager.bind_model_process(Invoice, BranchedProcess, state_field='status')
        self.addCleanup(ProcessManager.unbind_model_process, Invoice, BranchedProcess)
        cache.clear()
        self.addCleanup(cache.clear)
        _ran.clear()
        self.invoice = Invoice.objects.create(status='draft')

    def test_a_task_with_no_user_takes_the_automation_branch(self):
        self.invoice.branched.close()
        self.assertEqual(_ran, ['automation'])

    def test_a_staff_member_takes_the_person_branch(self):
        self.invoice.branched.close(user=_Staff())
        self.assertEqual(_ran, ['person'])

    def test_a_guest_matches_neither_branch(self):
        with self.assertRaises(TransitionNotAllowed):
            self.invoice.branched.close(user=_Guest())
        self.assertEqual(_ran, [])
