"""R6 regressions — phase-2 restore resolves the process phase 1 enqueued.

Every Process defaults to ``process_name='process'``, so phase 2's
attribute lookup (``getattr(instance, tm.process_name)``) can silently
resolve a *different* class than the one that enqueued the transition —
a directly-instantiated process colliding with the bound one, or a
rename/rebind between the deploy that ran phase 1 and the one running
phase 2. Pre-fix, phase 2 then ran the bound process's side-effects (code
the caller never asked for) and reported success. Post-fix, ``_restore``
verifies the resolved class against the recorded ``process_class`` and
prefers the recorded one, using the ``field_name`` recorded on the
message (with the legacy inference fallback for pre-0.4 rows).
"""
from django.test import TestCase, override_settings

from django_logic import Process
from django_logic.background import BackgroundTransition, sync_execution
from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from tests.background.models import Widget


_SYNC_SETTINGS = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}

# Module-level marker — proves WHICH process's side-effects ran.
RAN: list = []


def rogue_side_effect(instance, **kwargs):
    RAN.append('rogue_side_effect')


class RogueProcess(Process):
    """Deliberately collides with the bound WidgetProcess: same
    ``process_name`` AND a background transition with the same
    ``action_name`` ('fulfil') — but a different target and side-effects.
    Never bound to Widget; only ever instantiated directly."""

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


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class RestoreVerificationTests(TestCase):
    def setUp(self):
        RAN.clear()
        self.widget = Widget.objects.create()

    def test_name_collision_restores_the_recorded_process_class(self):
        # R6: phase 1 through a directly-instantiated RogueProcess. The TM
        # records process_name='process' (colliding with the bound
        # WidgetProcess) and process_class='...RogueProcess'. Pre-fix,
        # phase 2 resolved the BOUND class and ran WidgetProcess.fulfil's
        # side-effects with target 'fulfilled'.
        process = RogueProcess(field_name='status', instance=self.widget)
        with self.assertLogs('django-logic.transition', level='WARNING') as logs:
            with sync_execution():
                process.fulfil()

        self.widget.refresh_from_db()
        # The rogue transition ran — not the bound one.
        self.assertEqual(self.widget.status, 'rogue_fulfilled')
        self.assertEqual(RAN, ['rogue_side_effect'])
        self.assertEqual(self.widget.se_log, '')  # bound side-effects didn't run
        self.assertTrue(any('using the recorded class' in line for line in logs.output))

        tm = TransitionMessage.objects.get(instance_id=str(self.widget.pk))
        self.assertTrue(tm.is_completed)
        self.assertEqual(tm.field_name, 'status')

    def test_unimportable_recorded_class_falls_back_to_bound_process(self):
        # The recorded class vanished (deploy renamed it). Phase 2 logs an
        # error and falls back to the attribute-resolved bound process.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        tm = TransitionMessage.objects.create(
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
            run_background_transition(tm.pk)

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilled')  # bound transition ran
        self.assertIn('ok,', self.widget.se_log)
        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
        self.assertTrue(
            any('could not be loaded' in line for line in logs.output)
        )

    def test_phase_one_records_the_bound_field_name(self):
        with sync_execution():
            self.widget.process.fulfil()
        tm = TransitionMessage.objects.get(instance_id=str(self.widget.pk))
        self.assertEqual(tm.field_name, 'status')

    def test_legacy_row_without_field_name_uses_inference(self):
        # Pre-0.4 rows have field_name=''. On a name collision the recorded
        # class must still load, inferring the field from the bound process.
        self.widget.status = 'rogue_fulfilling'
        self.widget.save(update_fields=['status'])
        tm = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            field_name='',  # legacy row
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={
                'process_class':
                    'tests.background.test_restore_verification.RogueProcess',
            },
        )

        run_background_transition(tm.pk)

        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'rogue_fulfilled')
        self.assertEqual(RAN, ['rogue_side_effect'])
        tm.refresh_from_db()
        self.assertTrue(tm.is_completed)
