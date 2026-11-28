"""Workers use current business records and users when normal reads use a replica."""
from django.contrib.auth import get_user_model
from django.db import connections, router
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import finalize_stuck_attempt, run_background_transition
from django_logic.background.serializers import restore_user
from tests import dl_settings
from tests.background import models as bg_models
from tests.background.models import Widget


class ReplicaRouter:
    def db_for_read(self, model, **hints):
        if model is Widget or model is get_user_model():
            return 'other'
        return None

    def db_for_write(self, model, **hints):
        return 'default'


class ActorReplicaRouter(ReplicaRouter):
    def db_for_read(self, model, **hints):
        return 'other' if model is get_user_model() else None


@override_settings(DJANGO_LOGIC=dl_settings(
    TRANSITION_MESSAGE_MAX_ERRORS=1, TRANSITION_MESSAGE_RETRY_MINUTES=0,
))
class ReadReplicaWorkerTests(TestCase):
    databases = {'default', 'other'}

    def setUp(self):
        bg_models.LAST_KWARGS.clear()
        self.addCleanup(bg_models.LAST_KWARGS.clear)
        self.widget = Widget.objects.create()
        self.user = get_user_model().objects.create(username='primary-actor')

    def enqueue(self, action='fulfil', *, user=None):
        with override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull')):
            getattr(self.widget.process, action)(user=user)
        return TransitionMessage.objects.get(instance_id=str(self.widget.pk))

    def assert_completed_once(self, job):
        job.refresh_from_db(using='default')
        self.widget.refresh_from_db(using='default')
        self.assertTrue(job.is_completed)
        self.assertFalse(job.ended_in_failure)
        self.assertEqual(job.errors_count, 0)
        self.assertEqual(self.widget.status, 'fulfilled')
        self.assertEqual(self.widget.se_log, 'ok,')
        self.assertEqual(self.widget.cb_log, 'cb,')

    @override_settings(DATABASE_ROUTERS=[ReplicaRouter()])
    def test_new_business_record_completes_without_reading_replica(self):
        self.assertEqual(router.db_for_read(Widget), 'other')
        self.assertEqual(router.db_for_write(Widget), 'default')
        self.assertEqual(router.db_for_read(TransitionMessage), 'default')
        self.assertFalse(Widget.objects.exists())
        job = self.enqueue()
        with CaptureQueriesContext(connections['other']) as replica_queries:
            run_background_transition(job.pk)
        self.assert_completed_once(job)
        self.assertEqual(len(replica_queries), 0)

    def test_stale_business_record_does_not_replace_current_state(self):
        job = self.enqueue()
        Widget.objects.using('other').create(pk=self.widget.pk, status='draft', se_log='old')
        with override_settings(DATABASE_ROUTERS=[ReplicaRouter()]):
            run_background_transition(job.pk)
        self.assert_completed_once(job)
        replica = Widget.objects.using('other').get(pk=self.widget.pk)
        self.assertEqual((replica.status, replica.se_log), ('draft', 'old'))

    def test_new_actor_is_restored_from_primary(self):
        job = self.enqueue(user=self.user)
        with override_settings(DATABASE_ROUTERS=[ActorReplicaRouter()]):
            self.assertFalse(get_user_model().objects.exists())
            run_background_transition(job.pk)
        self.assert_completed_once(job)
        actor = bg_models.LAST_KWARGS['user']
        self.assertEqual((actor.pk, actor._state.db), (self.user.pk, 'default'))

    def test_deleted_primary_actor_is_not_restored_from_stale_replica(self):
        job = self.enqueue(user=self.user)
        actor_id = self.user.pk
        get_user_model().objects.using('other').create(pk=actor_id, username='stale-actor')
        self.user.delete()
        with override_settings(DATABASE_ROUTERS=[ActorReplicaRouter()]):
            run_background_transition(job.pk)
        job.refresh_from_db(using='default')
        self.widget.refresh_from_db(using='default')
        self.assertTrue(job.is_completed)
        self.assertTrue(job.ended_in_failure)
        self.assertIn('[unrestorable]', job.last_error_message)
        self.assertIn('submitting user', job.last_error_message)
        self.assertEqual(job.kwargs['user_id'], actor_id)
        self.assertEqual((self.widget.status, self.widget.se_log, self.widget.cb_log), ('fulfilling', '', ''))
        self.assertEqual(bg_models.LAST_KWARGS, {})

    def test_deleted_primary_business_record_is_not_restored_from_replica(self):
        job = self.enqueue()
        Widget.objects.using('other').create(pk=self.widget.pk, status='fulfilling')
        self.widget.delete()
        with override_settings(DATABASE_ROUTERS=[ReplicaRouter()]):
            run_background_transition(job.pk)
        job.refresh_from_db(using='default')
        self.assertTrue(job.is_completed)
        self.assertTrue(job.ended_in_failure)
        self.assertIn('[unrestorable]', job.last_error_message)
        replica = Widget.objects.using('other').get(pk=int(job.instance_id))
        self.assertEqual((replica.status, replica.se_log, replica.cb_log), ('fulfilling', '', ''))
        self.assertEqual(bg_models.LAST_KWARGS, {})

    def test_failed_attempt_records_business_failure_on_primary(self):
        job = self.enqueue('crash', user=self.user)
        with override_settings(DATABASE_ROUTERS=[ReplicaRouter()]):
            with self.assertRaisesMessage(ValueError, 'boom'):
                run_background_transition(job.pk)
        job.refresh_from_db(using='default')
        self.widget.refresh_from_db(using='default')
        self.assertTrue(job.is_completed)
        self.assertTrue(job.ended_in_failure)
        self.assertEqual(job.errors_count, 1)
        self.assertEqual(self.widget.status, 'crash_failed')
        self.assertEqual(self.widget.cb_log, 'fcb,')

    def test_failure_maintenance_reads_current_business_record_and_actor(self):
        job = self.enqueue(user=self.user)
        TransitionMessage.objects.filter(pk=job.pk).update(errors_count=1)
        with override_settings(DATABASE_ROUTERS=[ReplicaRouter()]):
            self.assertTrue(finalize_stuck_attempt(job.pk))
        job.refresh_from_db(using='default')
        self.widget.refresh_from_db(using='default')
        self.assertTrue(job.is_completed)
        self.assertTrue(job.ended_in_failure)
        self.assertEqual(self.widget.status, 'fulfilment_failed')
        self.assertEqual(self.widget.cb_log, 'fcb,')

    def test_actor_uses_its_own_write_database(self):
        class SeparateActorRouter:
            def db_for_read(self, model, **hints):
                return 'default'

            def db_for_write(self, model, **hints):
                return 'other' if model is get_user_model() else 'default'

        get_user_model().objects.using('other').create(pk=self.user.pk, username='authoritative-actor')
        kwargs = {'user_id': self.user.pk}
        with override_settings(DATABASE_ROUTERS=[SeparateActorRouter()]):
            restore_user(kwargs)
        self.assertEqual(kwargs['user'].username, 'authoritative-actor')
        self.assertEqual(kwargs['user']._state.db, 'other')
