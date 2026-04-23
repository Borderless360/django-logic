"""Class-time validation: queue required, in_progress_state unique,
background action_names unique within a Process."""
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from django_logic import Process, Transition
from django_logic.background import BackgroundAction, BackgroundTransition


class QueueRequiredTests(SimpleTestCase):
    def test_background_transition_requires_queue(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            BackgroundTransition(
                action_name='x',
                sources=['a'],
                target='b',
                queue='',
            )
        self.assertIn("non-empty 'queue'", str(ctx.exception))

    def test_background_action_requires_queue(self):
        with self.assertRaises(ImproperlyConfigured):
            BackgroundAction(action_name='x', sources=['a'], queue='')

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
        self.assertIn("'a'", msg)
        self.assertIn("'b'", msg)

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
    """Phase-2 restore keys on ``action_name`` alone, so a Process must
    not contain two background transitions with the same name, nor a
    background transition colliding with a sync one.
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

    def test_sync_transition_collides_with_background_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            class _BadProcess(Process):
                process_name = 'bad_sync_bg'
                transitions = [
                    Transition(
                        action_name='fulfil', sources=['a'], target='b',
                    ),
                    BackgroundTransition(
                        action_name='fulfil',
                        sources=['a'],
                        target='b',
                        in_progress_state='fulfilling',
                        queue='q',
                    ),
                ]
        msg = str(ctx.exception)
        self.assertIn('synchronous Transition', msg)
        self.assertIn("'fulfil'", msg)

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
