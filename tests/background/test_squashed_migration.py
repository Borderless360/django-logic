"""SQL inspection and upgrades accept the retained original migration history."""
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

from django_logic.background.models import TransitionMessage


class MigrationSqlTests(TransactionTestCase):
    def test_library_migration_provides_forward_and_reverse_sql(self):
        for backwards, operation in ((False, 'CREATE TABLE'), (True, 'DROP TABLE')):
            with self.subTest(backwards=backwards):
                output = StringIO()
                call_command(
                    'sqlmigrate', 'django_logic_background', '0020',
                    backwards=backwards, stdout=output,
                )
                self.assertIn(operation, output.getvalue())
                self.assertIn('django_logic_background_jobprocessowner', output.getvalue())
                self.assertIn('django_logic_background_statelock', output.getvalue())

    def test_consumer_migration_provides_sql(self):
        output = StringIO()
        call_command('sqlmigrate', 'bg_tests', '0001', stdout=output)
        self.assertIn('CREATE TABLE', output.getvalue())
        self.assertIn('bg_tests_widget', output.getvalue())


class OriginalMigrationHistoryTests(TransactionTestCase):
    app_label = 'django_logic_background'
    squash_name = '0001_squashed_0010_proxy_model_label'
    original_names = (
        '0001_initial',
        '0002_transitionmessage_timing',
        '0003_transitionmessage_failure_side_effect_error',
        '0004_transitionmessage_timeout_seconds',
        '0005_transitionmessage_instance_id_text',
        '0006_per_process_constraint_and_field_name',
        '0007_transitionmessage_owning_process_class',
        '0008_transitionmessage_dispatch_marker',
        '0009_remove_dispatch_marker',
        '0010_proxy_model_label',
    )

    def setUp(self):
        self.latest = MigrationExecutor(connection).loader.graph.leaf_nodes(self.app_label)
        self.addCleanup(self.restore_latest)
        old_state = self.migrate([(self.app_label, self.squash_name)])
        self.jobs = old_state.apps.get_model(self.app_label, 'TransitionMessage')
        self.job = self.jobs.objects.create(
            app_label='bg_tests', model_name='widget', instance_id='409',
            process_name='process', transition_name='fulfil', queue_name='django_logic.critical',
            is_completed=True, errors_count=2, last_error_message='saved failure',
            kwargs={'saved_value': 'preserve this'},
        )

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets)

    def restore_latest(self):
        recorder = MigrationRecorder(connection)
        if (self.app_label, self.squash_name) not in recorder.applied_migrations():
            recorder.record_applied(self.app_label, self.squash_name)
        self.migrate(self.latest)

    def remove_squash_record(self):
        recorder = MigrationRecorder(connection)
        recorder.record_unapplied(self.app_label, self.squash_name)
        applied = recorder.applied_migrations()
        self.assertNotIn((self.app_label, self.squash_name), applied)
        for name in self.original_names:
            self.assertIn((self.app_label, name), applied)

    def assert_history_preserved(self):
        applied = MigrationRecorder(connection).applied_migrations()
        self.assertIn((self.app_label, self.squash_name), applied)
        for name in self.original_names:
            self.assertIn((self.app_label, name), applied)

    def test_original_history_upgrades_without_recreating_tables_or_losing_data(self):
        before = self.jobs.objects.values().get(pk=self.job.pk)
        self.remove_squash_record()

        executor = MigrationExecutor(connection)
        executor.loader.check_consistent_history(connection)
        plan = executor.migration_plan(self.latest)
        self.assertEqual(plan[0][0].name, '0011_operation_contract')
        self.assertNotIn(self.squash_name, [migration.name for migration, backwards in plan])

        self.migrate(self.latest)

        after = TransitionMessage.objects.values().get(pk=self.job.pk)
        self.assertEqual({name: after[name] for name in before}, before)
        self.assert_history_preserved()

    def test_later_applied_migrations_accept_missing_squash_record(self):
        self.migrate(self.latest)
        before = TransitionMessage.objects.values().get(pk=self.job.pk)
        self.remove_squash_record()

        executor = MigrationExecutor(connection)
        executor.loader.check_consistent_history(connection)
        self.assertEqual(executor.migration_plan(self.latest), [])
        executor.migrate(self.latest)

        self.assertEqual(TransitionMessage.objects.values().get(pk=self.job.pk), before)
        self.assert_history_preserved()
