"""Saved jobs restore the exact recorded process, owner and state field."""
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from django_logic import Process
from django_logic.background import BackgroundTransition, sync_execution
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import _restore, _RestoreError, run_background_transition
from django_logic.state import State
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
        with sync_execution():
            process.fulfil()

        self.widget.refresh_from_db()
        # The rogue transition ran, not the bound one.
        self.assertEqual(self.widget.status, 'rogue_fulfilled')
        self.assertEqual(RAN, ['rogue_side_effect'])
        self.assertEqual(self.widget.se_log, '')

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

    def test_unimportable_recorded_path_terminates(self):
        # A missing recorded class cannot select a different process.
        from django_logic.background.safety_nets import retry_pending

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
            self.assertEqual(retry_pending(), 0)
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
        # A missing field must not select another state on the same model.
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

    def test_missing_root_class_does_not_use_the_bound_process(self):
        message = TransitionMessage(
            app_label='bg_tests', model_name='widget', instance_id=str(self.widget.pk),
            process_name='process', field_name='status', transition_name='fulfil',
            owning_process_class='tests.background.models.WidgetProcess', kwargs={},
        )
        with self.assertRaisesMessage(_RestoreError, 'process_class and field_name'):
            _restore(message)
        self.assertEqual(RAN, [])

    def test_direct_submission_without_process_identity_changes_nothing(self):
        transition = RogueProcess.transitions[0]
        with self.assertRaisesMessage(ImproperlyConfigured, 'through its Process'):
            transition.change_state(State(self.widget, 'status', 'process'))
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')
        self.assertFalse(TransitionMessage.objects.exists())

    def test_direct_submission_with_complete_identity_runs(self):
        path = f'{__name__}.RogueProcess'
        RogueProcess.transitions[0].change_state(
            State(self.widget, 'status', 'process'),
            process_class=path, owning_process_class=path,
        )
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'rogue_fulfilled')
        self.assertEqual(RAN, ['rogue_side_effect'])

    def test_class_alias_and_changed_owner_are_refused_before_submission(self):
        path = f'{__name__}.RogueProcess'
        with patch(f'{__name__}.RogueAlias', RogueProcess, create=True):
            for root, owner in ((f'{__name__}.RogueAlias', path), (path, 'gone.OtherProcess')):
                with self.subTest(root=root, owner=owner):
                    with self.assertRaisesMessage(ImproperlyConfigured, 'identity cannot be restored'):
                        RogueProcess.transitions[0].change_state(
                            State(self.widget, 'status', 'process'),
                            process_class=root, owning_process_class=owner,
                        )
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')
        self.assertFalse(TransitionMessage.objects.exists())
