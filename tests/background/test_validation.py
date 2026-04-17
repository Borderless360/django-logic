"""Class-time validation: queue required, in_progress_state unique."""
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from django_logic import Process
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
