"""Class-time validation: queue optional but non-empty when given,
in_progress_state unique, background action_names unique within a Process."""
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from django_logic import Process, Transition
from django_logic.background import BackgroundAction, BackgroundTransition


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

    def test_background_action_rejects_empty_queue_string(self):
        with self.assertRaises(ImproperlyConfigured):
            BackgroundAction(action_name='x', sources=['a'], queue='')

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

    def test_background_action_rejects_in_progress_state(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            BackgroundAction(
                action_name='x',
                sources=['a'],
                queue='q',
                in_progress_state='processing',
            )
        self.assertIn('cannot declare in_progress_state', str(ctx.exception))


class UniqueInProgressStateTests(SimpleTestCase):
    def test_duplicate_in_progress_state_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            class _BadProcess(Process):
                process_name = 'bad'
                transitions = [
                    BackgroundTransition(
                        action_name='a',
                        sources=['s'],
                        target='t1',
                        in_progress_state='processing',
                        queue='q',
                    ),
                    BackgroundTransition(
                        action_name='b',
                        sources=['s'],
                        target='t2',
                        in_progress_state='processing',
                        queue='q',
                    ),
                ]
        msg = str(ctx.exception)
        self.assertIn("in_progress_state='processing'", msg)
        self.assertIn('_BadProcess.a', msg)
        self.assertIn('_BadProcess.b', msg)

    def test_duplicate_in_progress_state_across_nested_tree_rejected(self):
        # Issue #88: nested processes share the parent's state field, so the
        # uniqueness guarantee must hold across the whole tree — previously
        # only the class's own transitions were checked.
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

        with self.assertRaises(ImproperlyConfigured) as ctx:
            class _BadParent(Process):
                process_name = 'bad_parent'
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
        msg = str(ctx.exception)
        self.assertIn("in_progress_state='processing'", msg)
        self.assertIn('parent_act', msg)
        self.assertIn('child_act', msg)

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
                BackgroundAction(
                    action_name='a', sources=['s'], queue='q',
                ),
                BackgroundAction(
                    action_name='b', sources=['s'], queue='q',
                ),
            ]

        self.assertEqual(len(_LooseProcess.transitions), 2)


class UniqueBackgroundActionNameTests(SimpleTestCase):
    """Phase-2 restore identifies a background transition by ``(owning process
    class, action_name)`` and filters to ``is_background``, so the ONLY rejected
    configuration is two background transitions sharing a name within a single
    Process. Duplicate background names across *distinct* nested processes, and
    a background name coinciding with a synchronous one, are both allowed —
    resolved by conditions at phase 1, by the owner + is_background filter at
    phase 2.
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
                    BackgroundAction(
                        action_name='dup', sources=['s'], queue='q',
                    ),
                ]
        self.assertIn("action_name='dup'", str(ctx.exception))

    def test_sync_transition_sharing_name_with_background_allowed(self):
        # A synchronous transition may share an action_name with a background
        # one: phase 2 only restores background transitions (is_background
        # filter), so the synchronous namesake is invisible to restore; phase 1
        # resolves the call by conditions/permissions like any duplicate name.
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
        """Duplicate sync action_names remain legal — the sync call
        path disambiguates via conditions/permissions at runtime.
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
                BackgroundAction(
                    action_name='bg2', sources=['c'], queue='q',
                ),
            ]

        self.assertEqual(len(_GoodProcess.transitions), 3)


class NestedTreeBackgroundActionNameTests(SimpleTestCase):
    """Phase-2 ``_find_transition`` descends into ``nested_processes`` and
    selects the transition by ``(owning process class, action_name)``. So a
    background ``action_name`` may be reused across *distinct* nested processes
    (the condition-disambiguated pattern) and may coincide with a synchronous
    transition; it must only stay unique *within* any single process class.
    """

    def test_background_name_duplication_across_nested_processes_allowed(self):
        # Issue #98: the condition-disambiguated nested-process pattern. Two
        # nested processes each declare a background transition with the SAME
        # action_name, selected by a condition on the instance. Phase 1
        # resolves exactly one; phase 2 restores it via the recorded owning
        # process class. This must no longer raise at class creation.
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
        # Same as above but with BackgroundAction (no in_progress_state), so
        # the only discriminator is the owning process class.
        class _ChildA(Process):
            process_name = 'act_child_a'
            transitions = [
                BackgroundAction(action_name='dup', sources=['s'], queue='q'),
            ]

        class _ChildB(Process):
            process_name = 'act_child_b'
            transitions = [
                BackgroundAction(action_name='dup', sources=['s'], queue='q'),
            ]

        class _Parent(Process):
            process_name = 'parent_dup_bg_action'
            nested_processes = [_ChildA, _ChildB]

        self.assertEqual(_Parent.nested_processes, [_ChildA, _ChildB])

    def test_two_background_transitions_same_name_within_a_class_rejected(self):
        # Within-class duplication is still genuinely ambiguous: (owning class,
        # action_name) no longer identifies one transition. Each Process
        # validates itself at creation, so a process that nests such a child
        # raises when the CHILD class is defined.
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
        # A parent background transition may share an action_name with a nested
        # synchronous one: phase 2 (is_background filter) never restores the
        # synchronous transition, and phase 1 resolves the call by conditions.
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
        # Courier-style polymorphism: many nested sub-processes share a sync
        # action_name, disambiguated by conditions at runtime. With no
        # background transition involved, this stays legal.
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
