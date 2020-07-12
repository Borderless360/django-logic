from django.test import TestCase

from django_logic import Conditions, Process, Transition
from django_logic.display import get_object_id, get_conditions_id, get_readable_process_name, get_target_states, \
    get_all_target_states, get_all_states


class DisplayProcessTestCase(TestCase):
    def test_get_object_id(self):
        class A:
            pass
        a = A()
        self.assertEqual(get_object_id(a), str(id(a)))

    def test_get_conditions_id(self):
        conditions = Conditions()
        self.assertEqual(get_conditions_id(conditions),
                         get_object_id(conditions) + '|conditions')

    def test_get_readable_process_name(self):
        self.assertEqual(get_readable_process_name(Process), 'Process')

        class TestProcess(Process):
            pass

        self.assertEqual(get_readable_process_name(TestProcess), 'Test Process')

        class AnotherTestProcess(Process):
            pass

        self.assertEqual(get_readable_process_name(AnotherTestProcess), 'Another Test Process')

    def test_get_target_states(self):
        transition1 = Transition('cancel', sources=['draft'], target='done', failed_state='failed')
        transition2 = Transition('action', sources=['draft'], target='closed', failed_state='invalid')
        transition3 = Transition('bulk_action', sources=['draft'], target='closed', in_progress_state='closing')

        class ChildProcess(Process):
            transitions = [transition1, transition2, transition3]

        states = {'done', 'closed', 'failed', 'invalid', 'closing'}
        self.assertEqual(get_target_states(ChildProcess), states)

    def test_get_all_target_states(self):
        transition1 = Transition('cancel', sources=['draft'], target='done', failed_state='failed')
        transition2 = Transition('action', sources=['draft'], target='closed', failed_state='invalid')
        transition3 = Transition('bulk_action', sources=['draft'], target='closed', in_progress_state='closing')

        class ChildProcess(Process):
            transitions = [transition1, transition2, transition3]

        states = {'done', 'closed', 'failed', 'invalid', 'closing'}
        self.assertEqual(get_all_target_states(ChildProcess), states)

        transition4 = Transition('approve', sources=['draft'], target='approved',
                                 failed_state='declined', in_progress_state='approving')
        states |= {'approved', 'declined', 'approving'}

        class MainProcess(Process):
            transitions = [transition4, ]
            nested_processes = [ChildProcess, ]

        self.assertEqual(get_all_target_states(MainProcess), states)

    def test_get_all_states(self):
        transition1 = Transition('cancel', sources=['draft', 'open'], target='done', failed_state='failed')
        transition2 = Transition('action', sources=['draft'], target='closed', failed_state='invalid')
        transition3 = Transition('bulk_action', sources=['draft'], target='closed', in_progress_state='closing')

        class ChildProcess(Process):
            transitions = [transition1, transition2, transition3]

        states = {'done', 'closed', 'failed', 'invalid', 'closing', 'draft', 'open'}
        self.assertEqual(get_all_states(ChildProcess), states)

        transition4 = Transition('approve', sources=['draft', 'created'], target='approved',
                                 failed_state='declined', in_progress_state='approving')
        states |= {'approved', 'approving', 'declined', 'created'}

        class MainProcess(Process):
            transitions = [transition4, ]
            nested_processes = [ChildProcess, ]

        self.assertEqual(get_all_states(MainProcess), states)
