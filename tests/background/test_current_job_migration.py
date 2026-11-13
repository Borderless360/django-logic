"""The upgrade preserves unfinished work and removes unused physical columns."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

from django.db import OperationalError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from django_logic.background.models import TransitionMessage
from django_logic.background.runner import run_background_transition
from tests.background.models import Widget
from tests.stability.base import requires_postgres


class CurrentJobMigrationTests(TransactionTestCase):
    app_label = 'django_logic_background'

    def setUp(self):
        self.latest = MigrationExecutor(connection).loader.graph.leaf_nodes(self.app_label)
        old_state = self.migrate([(self.app_label, '0016_remove_unused_operation_fields')])
        self.jobs = old_state.apps.get_model(self.app_label, 'TransitionMessage')
        self.addCleanup(self.restore_latest)
        self.widget = Widget.objects.create(status='fulfilling')

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets)

    def restore_latest(self):
        self.jobs.objects.filter(is_completed=False).update(is_completed=True)
        self.migrate(self.latest)

    def job(self, **overrides):
        values = dict(
            app_label='bg_tests', model_name='widget', instance_id=str(self.widget.pk),
            process_name='process', field_name='status', transition_name='fulfil',
            owning_process_class='tests.background.models.WidgetProcess',
            queue_name='django_logic.critical',
            kwargs={'process_class': 'tests.background.models.WidgetProcess'},
        )
        values.update(overrides)
        return self.jobs.objects.create(**values)

    def columns(self):
        with connection.cursor() as cursor:
            return {column.name for column in connection.introspection.get_table_description(
                cursor, self.jobs._meta.db_table,
            )}

    def assert_refused(self, job):
        with self.assertRaisesMessage(RuntimeError, f'unfinished job #{job.pk}'):
            self.migrate(self.latest)
        job.refresh_from_db()
        self.widget.refresh_from_db()
        self.assertFalse(job.is_completed)
        self.assertEqual(job.errors_count, 0)
        self.assertEqual(self.widget.status, 'fulfilling')
        self.assertEqual(self.widget.se_log, '')
        self.assertTrue({'required_next', 'request_fingerprint'} <= self.columns())

    def test_current_unfinished_job_survives_and_runs(self):
        job = self.job()
        self.migrate(self.latest)
        self.assertFalse({'required_next', 'request_fingerprint'} & self.columns())
        self.assertFalse(TransitionMessage.objects.get(pk=job.pk).is_completed)
        run_background_transition(job.pk)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'fulfilled')
        self.assertEqual(self.widget.se_log, 'ok,')

    def test_direct_process_does_not_require_a_model_binding(self):
        path = 'tests.background.test_restore_verification.RogueProcess'
        job = self.job(owning_process_class=path, kwargs={'process_class': path})
        self.widget.status = 'rogue_fulfilling'
        self.widget.save(update_fields=['status'])
        self.migrate(self.latest)
        run_background_transition(job.pk)
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'rogue_fulfilled')

    def test_incomplete_or_changed_identity_refuses_without_changing_jobs(self):
        for changes in (
            {'kwargs': {}},
            {'owning_process_class': ''},
            {'field_name': ''},
            {'field_name': 'removed_status'},
            {'process_name': 'removed_process'},
            {'kwargs': {'process_class': 'tests.background.models.RemovedProcess'}},
            {'owning_process_class': 'tests.background.models.RemovedProcess'},
            {'transition_name': 'removed_action'},
            {'background_effect_names': ['removed_effect']},
            {'effect_name': 'removed_effect', 'parent_transition_name': 'fulfil'},
            {'request_key': 'old-operation-key'},
            {'model_name': 'widgetproxya'},
        ):
            with self.subTest(changes=changes):
                job = self.job(**changes)
                self.assert_refused(job)
                job.delete()

    def test_required_follow_up_refuses_without_changing_jobs(self):
        job = self.job()
        table = connection.ops.quote_name(self.jobs._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f'UPDATE {table} SET required_next = %s WHERE id = %s', ['notify', job.pk])
        self.assert_refused(job)

    def test_completed_history_does_not_require_old_declarations(self):
        job = self.job(is_completed=True, kwargs={}, owning_process_class='', field_name='')
        self.migrate(self.latest)
        self.assertTrue(TransitionMessage.objects.get(pk=job.pk).is_completed)
        self.assertFalse({'required_next', 'request_fingerprint'} & self.columns())

    @requires_postgres
    def test_schema_lock_wait_is_bounded_and_failed_upgrade_changes_nothing(self):
        job = self.job()
        locked, release = Event(), Event()

        def hold_job():
            try:
                with transaction.atomic():
                    self.jobs.objects.select_for_update().get(pk=job.pk)
                    locked.set()
                    release.wait(15)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            held = executor.submit(hold_job)
            try:
                self.assertTrue(locked.wait(5))
                started = time.monotonic()
                with self.assertRaises(OperationalError):
                    self.migrate(self.latest)
                self.assertLess(time.monotonic() - started, 8)
                self.assertTrue({'required_next', 'request_fingerprint'} <= self.columns())
                job.refresh_from_db()
                self.assertFalse(job.is_completed)
            finally:
                release.set()
                held.result(timeout=5)


class BackgroundEffectRemovalMigrationTests(TransactionTestCase):
    app_label = 'django_logic_background'
    removed_columns = {
        'request_key', 'background_effect_names', 'effect_name', 'parent_transition_name', 'parent_job_id',
    }
    migrate = CurrentJobMigrationTests.migrate
    restore_latest = CurrentJobMigrationTests.restore_latest
    job = CurrentJobMigrationTests.job
    columns = CurrentJobMigrationTests.columns

    def setUp(self):
        self.latest = MigrationExecutor(connection).loader.graph.leaf_nodes(self.app_label)
        old_state = self.migrate([(self.app_label, '0017_remove_legacy_job_columns')])
        self.jobs = old_state.apps.get_model(self.app_label, 'TransitionMessage')
        self.addCleanup(self.restore_latest)
        self.widget = Widget.objects.create(status='fulfilling')

    def test_unfinished_effect_metadata_refuses_without_changing_work_or_schema(self):
        for metadata in (
            {'background_effect_names': ['notify']},
            {'effect_name': 'notify'},
            {'parent_transition_name': 'fulfil'},
            {'parent_job_id': 123},
            {'request_key': 'django-logic:effect:123:notify'},
            {'request_key': 'old-operation-key'},
        ):
            with self.subTest(metadata=metadata):
                job = self.job(**metadata)
                before = self.jobs.objects.values().get(pk=job.pk)
                with self.assertRaisesMessage(RuntimeError, f'unfinished job #{job.pk}'):
                    self.migrate(self.latest)
                self.assertEqual(self.jobs.objects.values().get(pk=job.pk), before)
                self.assertTrue(self.removed_columns <= self.columns())
                self.widget.refresh_from_db()
                self.assertEqual((self.widget.status, self.widget.se_log), ('fulfilling', ''))
                job.delete()

    def test_compatible_pending_jobs_and_completed_history_are_preserved(self):
        pending = self.job(request_key='')
        history = self.job(
            is_completed=True, effect_name='notify', parent_transition_name='fulfil', parent_job_id=123,
            background_effect_names=['archive'], request_key='django-logic:effect:123:notify',
        )
        before = self.jobs.objects.values().get(pk=history.pk)

        self.migrate(self.latest)

        self.assertFalse(self.removed_columns & self.columns())
        self.assertEqual(
            TransitionMessage.objects.values().get(pk=history.pk),
            {key: value for key, value in before.items() if key not in self.removed_columns},
        )
        self.assertFalse(TransitionMessage.objects.get(pk=pending.pk).is_completed)
        run_background_transition(pending.pk)
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.status, self.widget.se_log), ('fulfilled', 'ok,'))

    @requires_postgres
    def test_effect_column_removal_has_a_bounded_schema_lock_wait(self):
        job = self.job()
        locked, release = Event(), Event()

        def hold_job():
            try:
                with transaction.atomic():
                    self.jobs.objects.select_for_update().get(pk=job.pk)
                    locked.set()
                    release.wait(15)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            held = executor.submit(hold_job)
            try:
                self.assertTrue(locked.wait(5))
                started = time.monotonic()
                with self.assertRaises(OperationalError):
                    self.migrate(self.latest)
                self.assertLess(time.monotonic() - started, 8)
                self.assertTrue(self.removed_columns <= self.columns())
                job.refresh_from_db()
                self.assertFalse(job.is_completed)
            finally:
                release.set()
                held.result(timeout=5)
