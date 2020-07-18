from django.test import TestCase

from django_logic import Conditions, Process, Transition
from django_logic.display import get_object_id, get_conditions_id, get_readable_process_name, get_target_states, \
    get_all_target_states, get_all_states, annotate_nodes


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


class AnnotateNodesTestCase(TestCase):
    def assertEqualNodes(self, nodes1, nodes2):
        """
        This method recursively tests the nodes
        """
        def compare_nodes(node1, node2):
            self.assertEqual(node1.keys(), node2.keys())
            for key in node1.keys():
                if key == 'nodes':
                    sorted_nodes1 = sorted(node1[key], key=lambda x: x['id'])
                    sorted_nodes2 = sorted(node2[key], key=lambda x: x['id'])
                    self.assertEqual(sorted_nodes1, sorted_nodes2)

                    for i in range(len(sorted_nodes1)):
                        compare_nodes(sorted_nodes1[i], sorted_nodes2[i])
                else:
                    self.assertEqual(node1[key], node2[key])

        compare_nodes(nodes1, nodes2)

    def test_empty_process(self):
        self.assertEqualNodes(annotate_nodes(Process), {
            'id': get_object_id(Process), 'name': 'Process', 'nodes': [], 'type': 'process'
        })

    def test_nested_processes_nodes(self):
        class ChildProcess(Process):
            pass

        class MainProcess(Process):
            nested_processes = [ChildProcess, ]

        self.assertEqualNodes(annotate_nodes(MainProcess), {
            'id': get_object_id(MainProcess), 'name': 'Main Process', 'type': 'process',
            'nodes': [{
                'id': get_object_id(ChildProcess),
                'name': 'Child Process',
                'nodes': [],
                'type': 'process'}],

        })

        class SuperProcess(Process):
            nested_processes = [MainProcess, ]

        self.assertEqualNodes(annotate_nodes(SuperProcess), {
            'id': get_object_id(SuperProcess), 'name': 'Super Process', 'type': 'process',
            'nodes': [{'id': get_object_id(MainProcess),
                       'name': 'Main Process',
                       'nodes': [{'id': get_object_id(ChildProcess),
                                  'name': 'Child Process',
                                  'nodes': [],
                                  'type': 'process'}],
                       'type': 'process'}],
            }
        )

    def test_process_permission_as_conditions_nodes(self):
        def is_staff(obj):
            return False

        def is_permitted(obj):
            return False

        class MainProcess(Process):
            permissions = [is_staff, is_permitted]

        self.assertEqualNodes(annotate_nodes(MainProcess), {
            'id': get_object_id(MainProcess), 'name': 'Main Process', 'type': 'process',
            'nodes': [{
                'id': f'{get_object_id(MainProcess)}|conditions',
                'name': 'is_staff\nis_permitted',
                'type': 'process_conditions'}
            ],

        })

    def test_process_conditions_nodes(self):
        def is_available(obj):
            return True

        def is_open(obj):
            return True

        class MainProcess(Process):
            conditions = [is_available, is_open]

        self.assertEqualNodes(annotate_nodes(MainProcess), {
            'id': get_object_id(MainProcess), 'name': 'Main Process', 'type': 'process',
            'nodes': [{
                'id': f'{get_object_id(MainProcess)}|conditions',
                'name': 'is_available\nis_open',
                'type': 'process_conditions'}
            ],
        })

    def test_transitions(self):
        transition1 = Transition('action', sources=['draft'], target='done')
        transition2 = Transition('action', sources=['draft'], target='closed')

        class MainProcess(Process):
            transitions = [transition1, transition2]

        nodes = annotate_nodes(MainProcess)

        self.assertEqualNodes(nodes, {
            'id': get_object_id(MainProcess), 'name': 'Main Process', 'type': 'process',
            'nodes': [
                {'id': get_object_id(transition1), 'name': 'action', 'type': 'transition'},
                {'id': get_object_id(transition2), 'name': 'action', 'type': 'transition'},
                {'id': 'closed', 'name': 'closed', 'type': 'state'},
                {'id': 'done', 'name': 'done', 'type': 'state'},
                {'id': 'draft', 'name': 'draft', 'type': 'state'}
            ]
        })

    def test_transition_permissions_as_conditions(self):
        def is_staff(obj):
            return False

        def is_permitted(obj):
            return False

        transition1 = Transition('action', sources=['draft'], target='done', permissions=[is_staff])
        transition2 = Transition('action', sources=['draft'], target='closed', permissions=[is_permitted])

        class MainProcess(Process):
            transitions = [transition1, transition2]

        self.assertEqualNodes(annotate_nodes(MainProcess), {
            'id': get_object_id(MainProcess), 'name': 'Main Process', 'type': 'process',
            'nodes': [
                {'id': get_object_id(transition1), 'name': 'action', 'type': 'transition'},
                {'id': get_object_id(transition2), 'name': 'action', 'type': 'transition'},
                {'id': f'{get_object_id(transition1)}|conditions', 'name': 'is_staff', 'type': 'transition_conditions'},
                {'id': f'{get_object_id(transition2)}|conditions', 'name': 'is_permitted', 'type': 'transition_conditions'},
                {'id': 'closed', 'name': 'closed', 'type': 'state'},
                {'id': 'done', 'name': 'done', 'type': 'state'},
                {'id': 'draft', 'name': 'draft', 'type': 'state'}
            ]})

    def test_transition_conditions(self):
        def is_available(obj):
            return True

        def is_open(obj):
            return True

        transition1 = Transition('action', sources=['draft'], target='done', conditions=[is_available, ])
        transition2 = Transition('action', sources=['draft'], target='closed', conditions=[is_open, ])

        class MainProcess(Process):
            transitions = [transition1, transition2]

        self.assertEqualNodes(annotate_nodes(MainProcess), {
            'id': get_object_id(MainProcess), 'name': 'Main Process', 'type': 'process',
            'nodes': [
                {'id': get_object_id(transition1), 'name': 'action', 'type': 'transition'},
                {'id': get_object_id(transition2), 'name': 'action', 'type': 'transition'},
                {'id': f'{get_object_id(transition1)}|conditions', 'name': 'is_available', 'type': 'transition_conditions'},
                {'id': f'{get_object_id(transition2)}|conditions', 'name': 'is_open', 'type': 'transition_conditions'},
                {'id': 'closed', 'name': 'closed', 'type': 'state'},
                {'id': 'done', 'name': 'done', 'type': 'state'},
                {'id': 'draft', 'name': 'draft', 'type': 'state'}
        ]})

    def test_intersect_states(self):
        transition1 = Transition('action', sources=['draft'], target='open')
        transition2 = Transition('action', sources=['draft'], target='closed')
        transition3 = Transition('action', sources=['draft'], target='closed')
        transition4 = Transition('action', sources=['draft'], target='open')

        class ChildProcess(Process):
            transitions = [transition1, transition2]

        class MainProcess(Process):
            transitions = [transition3]
            nested_processes = [ChildProcess]

        class SuperProcess(Process):
            transitions = [transition4]
            nested_processes = [MainProcess]

        self.assertEqualNodes(annotate_nodes(SuperProcess), {
            "id": get_object_id(SuperProcess), "name": "Super Process", "type": "process",
            "nodes": [
                {"id": get_object_id(transition4), "name": "action", "type": "transition"},
                {"id": "open", "name": "open", "type": "state"},
                {"id": "draft", "name": "draft", "type": "state"},
                {"id": get_object_id(MainProcess), "name": "Main Process", "type": "process",
                 "nodes": [
                     {"id": get_object_id(transition3), "name": "action", "type": "transition"},
                     {"id": "closed", "name": "closed", "type": "state"},
                     {"id": get_object_id(ChildProcess), "name": "Child Process", "type": "process",
                      "nodes": [
                          {"id": get_object_id(transition1), "name": "action", "type": "transition"},
                          {"id": get_object_id(transition2), "name": "action", "type": "transition"}
                      ]}
                 ]}
            ]}
        )
