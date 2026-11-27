"""Kwargs round-trip from enqueue to execute.

These pin what a background side-effect really receives on the worker.
Sync mode runs enqueue and execute inline, so one call covers both ends.
"""
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import finalize_stuck_attempt, run_background_transition
from django_logic.background.serializers import KwargsSerializationError
from tests.background import models as bg_models
from tests.background.models import Widget
from tests import dl_settings


_SYNC_SETTINGS = dl_settings(TRANSITION_MESSAGE_MAX_ERRORS=3)

_UUID = UUID('12345678-1234-5678-1234-567812345678')
_WHEN = datetime(2026, 6, 4, 12, 30, 0)


@override_settings(DJANGO_LOGIC=_SYNC_SETTINGS)
class KwargsRoundTripTests(TestCase):
    def setUp(self):
        bg_models.LAST_KWARGS.clear()
        self.widget = Widget.objects.create()
        self.user = get_user_model().objects.create(username='actor')

    def test_side_effect_receives_restored_user_and_context(self):
        self.widget.process.fulfil(
            user=self.user,
            when=_WHEN.isoformat(),
            some_uuid=str(_UUID),
        )
        seen = bg_models.LAST_KWARGS

        # The worker turns user_id back into a live User object.
        self.assertIn('user', seen)
        self.assertIsInstance(seen['user'], get_user_model())
        self.assertEqual(seen['user'].pk, self.user.pk)
        self.assertNotIn('user_id', seen)

        # The worker rebuilds context, so a side-effect declared as
        # fn(instance, context, **kwargs) works in both modes.
        self.assertIn('context', seen)
        self.assertEqual(seen['context'], {})

        self.assertEqual(seen['owning_process_class'], 'tests.background.models.WidgetProcess')
        transition_message = TransitionMessage.objects.get(
            transition_name='fulfil')
        self.assertEqual(
            transition_message.owning_process_class,
            'tests.background.models.WidgetProcess',
        )

    def test_request_refusal_reaches_the_caller_as_its_own_error(self):
        # Refused at the call, before the lock and before any write. The
        # serializer keeps its own refusal as defense for internal paths.
        with self.assertRaisesMessage(
                TypeError, 'never takes the request'):
            self.widget.process.fulfil(request=object())
        self.assertFalse(TransitionMessage.objects.exists())

    def test_nested_side_effect_receives_its_actual_declaring_class(self):
        self.widget.parent_process.nested_fulfil()
        seen = bg_models.LAST_KWARGS
        self.assertEqual(seen['owning_process_class'], 'tests.background.models.NestedBgChildProcess')
        transition_message = TransitionMessage.objects.get(
            transition_name='nested_fulfil')
        self.assertEqual(
            transition_message.owning_process_class,
            'tests.background.models.NestedBgChildProcess',
        )

    def test_non_json_argument_leaves_no_job(self):
        with self.assertRaisesRegex(ImproperlyConfigured, 'amount'):
            self.widget.process.fulfil(amount=Decimal('19.99'))
        self.assertFalse(TransitionMessage.objects.exists())

    def test_plain_json_strings_pass_through_unchanged(self):
        # Plain JSON strings remain strings; only tagged values are decoded.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            field_name='status',
            owning_process_class='tests.background.models.WidgetProcess',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'process_class': 'tests.background.models.WidgetProcess', 'when': _WHEN.isoformat(), 'some_uuid': str(_UUID)},
        )
        run_background_transition(transition_message.pk)
        seen = bg_models.LAST_KWARGS
        self.assertEqual(seen['when'], _WHEN.isoformat())
        self.assertEqual(seen['some_uuid'], str(_UUID))

    def test_malformed_tagged_row_fails_without_running_business_functions(self):
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        bad = {'__dl_type__': 'datetime', 'value': 'not-a-datetime'}
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            field_name='status',
            owning_process_class='tests.background.models.WidgetProcess',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'process_class': 'tests.background.models.WidgetProcess', 'when': bad},
        )
        for expected_errors in range(1, 4):
            with self.assertRaisesMessage(KwargsSerializationError, "legacy typed job argument"):
                run_background_transition(transition_message.pk)
            transition_message.refresh_from_db()
            self.assertEqual(transition_message.errors_count, expected_errors)
            self.assertEqual(transition_message.is_completed, expected_errors == 3)
            self.assertEqual(bg_models.LAST_KWARGS, {})
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilment_failed')

    def test_undecodable_kwargs_row_counts_errors_and_routes_failed_state(self):
        # A decode that really raises (a user_id that cannot be a primary key)
        # counts as an attempt failure. Otherwise errors_count stays at 0
        # and the row stays claimable forever.
        self.widget.status = 'fulfilling'
        self.widget.save(update_fields=['status'])
        transition_message = TransitionMessage.objects.create(
            app_label='bg_tests',
            model_name='widget',
            instance_id=str(self.widget.pk),
            process_name='process',
            field_name='status',
            owning_process_class='tests.background.models.WidgetProcess',
            transition_name='fulfil',
            queue_name='django_logic.critical',
            kwargs={'process_class': 'tests.background.models.WidgetProcess', 'user_id': ['not', 'a', 'pk']},
        )
        with self.assertRaises(TypeError):
            run_background_transition(transition_message.pk)
        transition_message.refresh_from_db()
        self.assertFalse(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 1)

        # Run the remaining attempts as the worker would. A row becomes
        # claimable again once RETRY_MINUTES have passed.
        for _ in range(2):
            with self.assertRaises(TypeError):
                run_background_transition(transition_message.pk)
        transition_message.refresh_from_db()
        self.assertTrue(transition_message.is_completed)
        self.assertEqual(transition_message.errors_count, 3)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilment_failed')

    def _submit_before_user_deletion(self):
        with override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull')):
            self.widget.process.fulfil(user=self.user)
        job = TransitionMessage.objects.get(instance_id=str(self.widget.pk))
        self.assertEqual(job.kwargs['user_id'], self.user.pk)
        self.user.delete()
        return job

    def _assert_deleted_user_stopped(self, job):
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertTrue(job.ended_in_failure)
        self.assertIn('[unrestorable]', job.last_error_message)
        self.assertIn('submitting user', job.last_error_message)
        self.assertIn('no longer exists', job.last_error_message)
        self.assertIn('user_id', job.kwargs)
        self.assertEqual(self.widget.status, 'fulfilling')
        self.assertEqual(self.widget.se_log, '')
        self.assertEqual(self.widget.cb_log, '')
        self.assertEqual(self.widget.kwargs_seen, [])
        self.assertEqual(bg_models.LAST_KWARGS, {})

    def test_deleted_user_stops_job_without_business_functions_or_retry(self):
        job = self._submit_before_user_deletion()
        run_background_transition(job.pk)
        self._assert_deleted_user_stopped(job)
        self.assertEqual(job.start_attempt_count, 1)
        run_background_transition(job.pk)
        self._assert_deleted_user_stopped(job)
        self.assertEqual(job.start_attempt_count, 1)

    def test_deleted_user_stops_failure_callbacks_during_maintenance(self):
        job = self._submit_before_user_deletion()
        job.errors_count = 3
        job.last_error_message = 'Earlier provider failure'
        job.save(update_fields=['errors_count', 'last_error_message'])
        self.assertTrue(finalize_stuck_attempt(job.pk))
        self._assert_deleted_user_stopped(job)
        self.assertIn('Earlier provider failure', job.last_error_message)

    def test_system_jobs_still_run_without_a_submitting_user(self):
        for kwargs in ({}, {'user': None}):
            with self.subTest(kwargs=kwargs):
                widget = Widget.objects.create()
                widget.process.fulfil(**kwargs)
                job = TransitionMessage.objects.get(instance_id=str(widget.pk))
                widget.refresh_from_db()
                self.assertTrue(job.is_completed)
                self.assertFalse(job.ended_in_failure)
                self.assertNotIn('user_id', job.kwargs)
                self.assertEqual(widget.status, 'fulfilled')
                self.assertEqual(widget.se_log, 'ok,')
                self.assertEqual(widget.cb_log, 'cb,')
