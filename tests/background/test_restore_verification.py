"""The worker restores the process that enqueued the transition.

Every Process defaults to ``process_name='process'``, so the worker's
attribute lookup can resolve a different class than the one that enqueued
the row: a directly instantiated process that collides with the bound one,
or a rename between two deploys. The worker therefore checks the resolved
class against the recorded ``process_class`` and prefers the recorded one.
"""
from django.test import TestCase, override_settings

from django_logic import Process
from django_logic.background import BackgroundTransition, sync_execution
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from tests.background.models import Widget
from tests import dl_settings


# Records which process's side-effects ran.
RAN: list = []


def rogue_side_effect(instance, **kwargs):
    RAN.append('rogue_side_effect')


class RogueProcess(Process):
    """Collides with the bound WidgetProcess on both ``process_name`` and
    ``action_name``, but has a different target and side-effects. It is never
    bound to Widget — tests instantiate it directly."""

    process_name = 'process'
    transitions = [
        BackgroundTransition(
            action_name='fulfil',
            sources=['draft'],
            target='rogue_fulfilled',
            in_progress_state='rogue_fulfilling',
            failed_state='rogue_failed',
            side_effects=[rogue_side_effect],
        ),
    ]


class RestoreVerificationTests(TestCase):
    def setUp(self):
        RAN.clear()
        self.widget = Widget.objects.create()

    def test_name_collision_restores_the_recorded_process_class(self):
        # Enqueue through a directly instantiated RogueProcess. Its row shares
        # process_name with the bound WidgetProcess, so only the recorded
        # process_class tells the worker which side-effects to run.
        process = RogueProcess(field_name='status', instance=self.widget)
        with self.assertLogs('django-logic.transition', level='WARNING') as logs:
            with sync_execution():
                process.fulfil()

        self.widget.refresh_from_db()
        # The rogue transition ran, not the bound one.
        self.assertEqual(self.widget.status, 'rogue_fulfilled')
        self.assertEqual(RAN, ['rogue_side_effect'])
        self.assertEqual(self.widget.se_log, '')
        self.assertTrue(any('using the recorded class' in line for line in logs.output))

        transition_message = TransitionMessage.objects.get(
            instance_id=str(self.widget.pk))
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.field_name, 'status')

    def test_unimportable_recorded_class_fails_closed(self):
        # The recorded class is gone: a deploy renamed it and no alias exists.
        # Falling back to the bound process would run side-effects the caller
        # never asked for, so the row completes as unrestorable with no
        # side-effects and no state write. last_error_message says why.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            field_name='status',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={
                'process_class':
                    'tests.background.test_restore_verification.DoesNotExist',
            },
        )

        with self.assertLogs('django-logic.transition', level='ERROR') as logs:
            run_background_transition(transition_message.pk)

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilling')
        self.assertEqual(self.widget.se_log, '')
        transition_message.refresh_from_db()
        # Completed, so retries stop.
        self.assertTrue(transition_message.is_completed)
        self.assertIn('[unrestorable]', transition_message.last_error_message)
        self.assertIn('could not be loaded',
                      transition_message.last_error_message)
        self.assertIsNotNone(transition_message.last_error_dt)
        self.assertTrue(
            any('could not be loaded' in line for line in logs.output)
        )

    def test_unimportable_path_through_the_attribute_fallback_terminates(self):
        # The instance has no attribute for the recorded process_name, so
        # restore falls back to process_class — which is also unimportable.
        # The row must terminate here. Earlier the ImportError escaped, the
        # attempt rolled back with errors_count still 0, and the periodic
        # starter sent the row to the queue again forever.
        from django_logic.background.safety_nets import run_pending

        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='no_such_process_attr',
            field_name='status',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={
                'process_class': 'tests.no.such.module.GhostProcess',
            },
        )

        with self.assertLogs('django-logic.transition', level='ERROR'):
            run_background_transition(transition_message.pk)

        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        # Unrestorable, not failing, so no error is charged.
        self.assertEqual(transition_message.errors_count, 0)
        self.assertIn('[unrestorable]', transition_message.last_error_message)

        # The starter has nothing left to send to the queue again.
        with override_settings(
                DJANGO_LOGIC=dl_settings(TRANSITION_MESSAGE_RETRY_MINUTES=0)):
            self.assertEqual(run_pending(), 0)
        transition_message.refresh_from_db()
        self.assertEqual(transition_message.errors_count, 0,
                         'errors must not grow')
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilling',
                         'no substitute side-effects, no state write')

    def test_enqueue_records_the_bound_field_name(self):
        with sync_execution():
            self.widget.process.fulfil()
        transition_message = TransitionMessage.objects.get(
            instance_id=str(self.widget.pk))
        self.assertEqual(transition_message.field_name, 'status')

    def test_row_without_field_name_fails_closed(self):
        # Enqueue has recorded field_name since 0.4. A row without one is
        # unrestorable: guessing 'state' could drive the wrong machine on a
        # model with several processes, so it terminates instead of running
        # hooks.
        self.widget.status = 'rogue_fulfilling'
        self.widget.save(update_fields=['status'])
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            field_name='',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={
                'process_class':
                    'tests.background.test_restore_verification.RogueProcess',
            },
        )

        run_background_transition(transition_message.pk)

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'rogue_fulfilling')
        self.assertEqual(RAN, [])
        transition_message.refresh_from_db()
        # Completed, so the retry loop stops.
        self.assertTrue(transition_message.is_completed)
        self.assertIn('[unrestorable]', transition_message.last_error_message)
