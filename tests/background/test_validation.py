"""Validation at class creation: ``queue`` is optional but must not be empty,
and background ``action_name`` values must be unique inside one Process.
Sharing an ``in_progress_state`` is legal; tests/test_binding_validation.py
covers the rule that replaced the old uniqueness check.
"""
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from django_logic import Process, Transition
from django_logic.background import BackgroundTransition


class QueueValidationTests(SimpleTestCase):
    def test_empty_queue_string_rejected(self):
        # queue is optional (DEFAULT_QUEUE applies), but an explicit empty
        # string is a typo, not a request for the default.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            BackgroundTransition(
                action_name='x',
                sources=['a'],
                target='b',
                queue='',
            )
        self.assertIn('non-empty string', str(ctx.exception))

    def test_queue_defaults_to_default_queue_setting(self):
        transition = BackgroundTransition(
            action_name='x', sources=['a'], target='b'
        )
        self.assertIsNone(transition.queue)
        self.assertEqual(transition.get_queue_name(), 'django_logic')
        with override_settings(DJANGO_LOGIC={'DEFAULT_QUEUE': 'my.queue'}):
            self.assertEqual(transition.get_queue_name(), 'my.queue')

    def test_declared_queue_wins_over_default(self):
        transition = BackgroundTransition(
            action_name='x', sources=['a'], target='b', queue='critical'
        )
        self.assertEqual(transition.get_queue_name(), 'critical')

    def test_no_target_rejects_in_progress_state(self):
        # Success writes no state, so the instance would stay parked in
        # the in-progress state forever.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            BackgroundTransition(
                action_name='x',
                sources=['a'],
                queue='q',
                in_progress_state='processing',
            )
        self.assertIn('in_progress_state needs a target', str(ctx.exception))


class SharedInProgressStateTests(SimpleTestCase):
    def test_duplicate_in_progress_state_accepted(self):
        class _SharedProcess(Process):
            process_name = 'shared'
            transitions = [
                BackgroundTransition(
                    action_name='a',
                    sources=['s'],
                    target='t1',
                    in_progress_state='processing',
                    failed_state='oops',
                    queue='q',
                ),
                BackgroundTransition(
                    action_name='b',
                    sources=['s'],
                    target='t2',
                    in_progress_state='processing',
                    failed_state='oops',
                    queue='q',
                ),
            ]

        self.assertEqual(
            {t.in_progress_state for t in _SharedProcess.transitions},
            {'processing'},
        )

    def test_duplicate_in_progress_state_across_nested_tree_accepted(self):
        class _NestedChild(Process):
            process_name = 'child'
            transitions = [
                BackgroundTransition(
                    action_name='child_act',
                    sources=['s'],
                    target='t1',
                    in_progress_state='processing',
                    queue='q',
                ),
            ]

        class _SharedParent(Process):
            process_name = 'shared_parent'
            nested_processes = [_NestedChild]
            transitions = [
                BackgroundTransition(
                    action_name='parent_act',
                    sources=['s'],
                    target='t2',
                    in_progress_state='processing',
                    queue='q',
                ),
            ]

        self.assertEqual(_SharedParent.nested_processes, [_NestedChild])

    def test_unique_in_progress_states_accepted(self):
        class _GoodProcess(Process):
            process_name = 'good'
            transitions = [
                BackgroundTransition(
                    action_name='a',
                    sources=['s'],
                    target='t1',
                    in_progress_state='one',
                    queue='q',
                ),
                BackgroundTransition(
                    action_name='b',
                    sources=['s'],
                    target='t2',
                    in_progress_state='two',
                    queue='q',
                ),
            ]

        self.assertEqual(len(_GoodProcess.transitions), 2)

    def test_missing_in_progress_state_not_validated(self):
        # Transitions without in_progress_state are allowed even if multiple.
        class _LooseProcess(Process):
            process_name = 'loose'
            transitions = [
                BackgroundTransition(
                    action_name='a', sources=['s'], queue='q',
                ),
                BackgroundTransition(
                    action_name='b', sources=['s'], queue='q',
                ),
            ]

        self.assertEqual(len(_LooseProcess.transitions), 2)


class UniqueBackgroundActionNameTests(SimpleTestCase):
    """The worker finds a background transition by its process class and its
    ``action_name``, so only two background transitions with the same name in
    one Process are ambiguous. The same name in two different nested processes,
    or shared with a synchronous transition, stays legal.
    """

    def test_two_background_transitions_same_name_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            class _BadProcess(Process):
                process_name = 'bad_bg_bg'
                transitions = [
                    BackgroundTransition(
                        action_name='dup',
                        sources=['s'],
                        target='t1',
                        in_progress_state='one',
                        queue='q',
                    ),
                    BackgroundTransition(
                        action_name='dup',
                        sources=['s'],
                        target='t2',
                        in_progress_state='two',
                        queue='q',
                    ),
                ]
        msg = str(ctx.exception)
        self.assertIn("action_name='dup'", msg)
        self.assertIn('background action_names must be unique', msg)

    def test_background_action_collides_with_background_transition(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            class _BadProcess(Process):
                process_name = 'bad_act_tr'
                transitions = [
                    BackgroundTransition(
                        action_name='dup',
                        sources=['s'],
                        target='t',
                        in_progress_state='one',
                        queue='q',
                    ),
                    BackgroundTransition(
                        action_name='dup', sources=['s'], queue='q',
                    ),
                ]
        self.assertIn("action_name='dup'", str(ctx.exception))

    def test_sync_transition_sharing_name_with_background_allowed(self):
        # The worker only restores background transitions, so a synchronous
        # transition with the same name is invisible to it. The call itself
        # resolves by conditions, like any other duplicate name.
        class _MixedProcess(Process):
            process_name = 'sync_bg_share'
            transitions = [
                Transition(
                    action_name='fulfil',
                    sources=['a'],
                    target='b',
                    conditions=[lambda i, **k: False],
                ),
                BackgroundTransition(
                    action_name='fulfil',
                    sources=['a'],
                    target='b',
                    in_progress_state='fulfilling',
                    conditions=[lambda i, **k: True],
                    queue='q',
                ),
            ]

        self.assertEqual(len(_MixedProcess.transitions), 2)

    def test_sync_transitions_same_name_still_allowed(self):
        """Duplicate synchronous action_names stay legal: conditions and
        permissions pick one at call time.
        """
        class _SyncDupProcess(Process):
            process_name = 'sync_dup'
            transitions = [
                Transition(action_name='x', sources=['a'], target='b'),
                Transition(action_name='x', sources=['c'], target='d'),
            ]

        self.assertEqual(len(_SyncDupProcess.transitions), 2)

    def test_unique_names_across_types_accepted(self):
        class _GoodProcess(Process):
            process_name = 'mixed_ok'
            transitions = [
                Transition(action_name='sync1', sources=['a'], target='b'),
                BackgroundTransition(
                    action_name='bg1',
                    sources=['b'],
                    target='c',
                    in_progress_state='b_to_c',
                    queue='q',
                ),
                BackgroundTransition(
                    action_name='bg2', sources=['c'], queue='q',
                ),
            ]

        self.assertEqual(len(_GoodProcess.transitions), 3)


class NestedTreeBackgroundActionNameTests(SimpleTestCase):
    """The worker searches ``nested_processes`` and selects a transition by its
    process class and its ``action_name``. A background ``action_name`` must
    therefore be unique inside one process class, but it may repeat across
    different nested processes.
    """

    def test_background_name_duplication_across_nested_processes_allowed(self):
        # Two nested processes declare the same background action_name and a
        # condition on the instance picks one. The enqueue resolves exactly one
        # transition, and the worker restores it from the process class on the
        # row.
        class _ChildA(Process):
            process_name = 'child_a'
            transitions = [
                BackgroundTransition(
                    action_name='dup',
                    sources=['s'],
                    target='t',
                    in_progress_state='a_running',
                    queue='q',
                ),
            ]

        class _ChildB(Process):
            process_name = 'child_b'
            transitions = [
                BackgroundTransition(
                    action_name='dup',
                    sources=['s'],
                    target='t',
                    in_progress_state='b_running',
                    queue='q',
                ),
            ]

        class _Parent(Process):
            process_name = 'parent_dup_bg'
            nested_processes = [_ChildA, _ChildB]

        self.assertEqual(_Parent.nested_processes, [_ChildA, _ChildB])

    def test_background_action_duplication_across_nested_processes_allowed(self):
        # Same for two no-target transitions: the process class is the only
        # thing that tells the two apart.
        class _ChildA(Process):
            process_name = 'act_child_a'
            transitions = [
                BackgroundTransition(action_name='dup', sources=['s'], queue='q'),
            ]

        class _ChildB(Process):
            process_name = 'act_child_b'
            transitions = [
                BackgroundTransition(action_name='dup', sources=['s'], queue='q'),
            ]

        class _Parent(Process):
            process_name = 'parent_dup_bg_action'
            nested_processes = [_ChildA, _ChildB]

        self.assertEqual(_Parent.nested_processes, [_ChildA, _ChildB])

    def test_two_background_transitions_same_name_within_a_class_rejected(self):
        # Two of the same name inside one class stay ambiguous: the process
        # class and the action_name no longer identify one transition. Each
        # Process validates itself, so the child class raises when it is
        # defined.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            class _Child(Process):
                process_name = 'dup_within_child'
                transitions = [
                    BackgroundTransition(
                        action_name='dup',
                        sources=['s'],
                        target='t1',
                        in_progress_state='one',
                        queue='q',
                    ),
                    BackgroundTransition(
                        action_name='dup',
                        sources=['s'],
                        target='t2',
                        in_progress_state='two',
                        queue='q',
                    ),
                ]
        msg = str(ctx.exception)
        self.assertIn("action_name='dup'", msg)
        self.assertIn('within a single process class', msg)

    def test_parent_background_sharing_name_with_nested_sync_allowed(self):
        # The worker never restores a synchronous transition, so a nested
        # synchronous namesake cannot be mistaken for the parent's background
        # transition.
        class _Child(Process):
            process_name = 'sync_child'
            transitions = [
                Transition(
                    action_name='fulfil',
                    sources=['a'],
                    target='b',
                    conditions=[lambda i, **k: False],
                ),
            ]

        class _Parent(Process):
            process_name = 'parent_bg_vs_nested_sync'
            nested_processes = [_Child]
            transitions = [
                BackgroundTransition(
                    action_name='fulfil',
                    sources=['a'],
                    target='b',
                    in_progress_state='fulfilling',
                    conditions=[lambda i, **k: True],
                    queue='q',
                ),
            ]

        self.assertEqual(_Parent.nested_processes, [_Child])

    def test_distinct_background_names_across_nested_accepted(self):
        class _Child(Process):
            process_name = 'distinct_child'
            transitions = [
                BackgroundTransition(
                    action_name='child_bg',
                    sources=['s'],
                    target='t',
                    in_progress_state='child_running',
                    queue='q',
                ),
            ]

        class _Parent(Process):
            process_name = 'distinct_parent'
            nested_processes = [_Child]
            transitions = [
                BackgroundTransition(
                    action_name='parent_bg',
                    sources=['s'],
                    target='t',
                    in_progress_state='parent_running',
                    queue='q',
                ),
            ]

        self.assertEqual(_Parent.nested_processes, [_Child])

    def test_sync_name_duplication_across_nested_still_allowed(self):
        # Several nested processes share one synchronous action_name and
        # conditions pick one at call time. No background transition is
        # involved, so this stays legal.
        class _CourierA(Process):
            process_name = 'courier_a'
            transitions = [
                Transition(action_name='submit', sources=['a'], target='b'),
            ]

        class _CourierB(Process):
            process_name = 'courier_b'
            transitions = [
                Transition(action_name='submit', sources=['a'], target='b'),
            ]

        class _Dispatch(Process):
            process_name = 'dispatch'
            nested_processes = [_CourierA, _CourierB]
            transitions = [
                Transition(action_name='submit', sources=['a'], target='b'),
            ]

        self.assertEqual(len(_Dispatch.nested_processes), 2)
