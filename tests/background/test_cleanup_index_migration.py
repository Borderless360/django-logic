"""The cleanup index preserves jobs and permits writes during PostgreSQL migration."""
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path
import subprocess
import sys
from textwrap import dedent
from threading import Event
import time

from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.writer import MigrationWriter
from django.test import SimpleTestCase, TransactionTestCase

from tests.stability.base import requires_postgres


class CleanupIndexMigrationTests(TransactionTestCase):
    app_label = 'django_logic_background'
    old_target = [('django_logic_background', '0020_state_lock_ownership')]
    new_target = [('django_logic_background', '0021_completed_job_cleanup_index')]
    index_name = 'dl_bg_cleanup_age_idx'

    def setUp(self):
        self.latest = MigrationExecutor(connection).loader.graph.leaf_nodes(self.app_label)
        self.addCleanup(self.migrate, self.latest)
        old_state = self.migrate(self.old_target)
        self.jobs = old_state.apps.get_model(self.app_label, 'TransitionMessage')

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets)

    def job(self, instance_id, **overrides):
        values = dict(
            app_label='bg_tests', model_name='widget', instance_id=instance_id,
            process_name='process', transition_name='fulfil', queue_name='django_logic.critical',
            kwargs={'saved_value': instance_id},
        )
        values.update(overrides)
        return self.jobs.objects.create(**values)

    def indexes(self):
        with connection.cursor() as cursor:
            return connection.introspection.get_constraints(cursor, self.jobs._meta.db_table)

    def assert_index_valid(self):
        index = self.indexes()[self.index_name]
        self.assertTrue(index['index'])
        self.assertEqual(index['columns'], ['modified', 'id'])
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT indisvalid FROM pg_index WHERE indexrelid = %s::regclass',
                    [self.index_name],
                )
                self.assertTrue(cursor.fetchone()[0])

    def test_apply_and_reverse_preserve_pending_completed_and_failed_jobs(self):
        self.job('pending')
        self.job('completed', is_completed=True, completion_kind='succeeded')
        self.job('failed', is_completed=True, ended_in_failure=True, errors_count=2,
                 last_error_message='saved failure', completion_kind='failed')
        before = list(self.jobs.objects.order_by('pk').values())
        self.assertNotIn(self.index_name, self.indexes())

        self.migrate(self.new_target)

        self.assert_index_valid()
        self.assertEqual(list(self.jobs.objects.order_by('pk').values()), before)

        self.migrate(self.old_target)

        self.assertNotIn(self.index_name, self.indexes())
        self.assertEqual(list(self.jobs.objects.order_by('pk').values()), before)

    def test_sql_builds_and_drops_concurrently_only_on_postgresql(self):
        loader = MigrationExecutor(connection).loader
        migration = loader.get_migration(*self.new_target[0])
        forwards = '\n'.join(loader.collect_sql([(migration, False)]))
        backwards = '\n'.join(loader.collect_sql([(migration, True)]))
        concurrent = ' CONCURRENTLY' if connection.vendor == 'postgresql' else ''
        self.assertIn(f'CREATE INDEX{concurrent} "{self.index_name}"', forwards)
        self.assertIn('("modified", "id") WHERE "is_completed"', forwards)
        self.assertIn(f'DROP INDEX{concurrent}', backwards)
        self.assertIn(f'"{self.index_name}"', backwards)
        if connection.vendor != 'postgresql':
            self.assertNotIn('CONCURRENTLY', forwards + backwards)

    @requires_postgres
    def test_job_writes_continue_while_the_index_is_being_built(self):
        existing = self.job('existing', is_completed=True)
        migration_ready = Event()
        migration_process = []

        def build_index():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '10s'")
                    cursor.execute('SELECT pg_backend_pid()')
                    migration_process.append(cursor.fetchone()[0])
                migration_ready.set()
                self.migrate(self.new_target)
            finally:
                connections.close_all()

        def write_job():
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '500ms'")
                    return self.job('written-during-index', is_completed=True).pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            with transaction.atomic():
                self.jobs.objects.filter(pk=existing.pk).update(last_error_message='writer still open')
                building = executor.submit(build_index)
                self.assertTrue(migration_ready.wait(5))
                deadline = time.monotonic() + 5
                while True:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            'SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s '
                            "AND relation = %s::regclass AND mode = 'ShareUpdateExclusiveLock' AND granted)",
                            [migration_process[0], self.jobs._meta.db_table],
                        )
                        if cursor.fetchone()[0]:
                            break
                    if building.done():
                        building.result()
                        self.fail('The index build finished before the first writer committed.')
                    if time.monotonic() >= deadline:
                        self.fail('The concurrent index build did not acquire its table lock.')
                    Event().wait(0.01)
                written_id = executor.submit(write_job).result(timeout=5)
                self.assertTrue(self.jobs.objects.filter(pk=written_id).exists())
                self.assertFalse(building.done())
            building.result(timeout=10)

        self.assert_index_valid()
        self.assertTrue(self.jobs.objects.filter(pk=written_id).exists())


class CleanupIndexDependencyTests(SimpleTestCase):
    def test_operation_serializes_with_its_importable_helper(self):
        module = import_module('django_logic.migrations.0021_completed_job_cleanup_index')
        migration = module.Migration('0021_completed_job_cleanup_index', 'django_logic_background')
        namespace = {}
        exec(MigrationWriter(migration).as_string(), namespace)
        self.assertIs(type(namespace['Migration'].operations[0]), type(migration.operations[0]))

    def test_sqlite_migrates_without_postgresql_drivers(self):
        script = dedent('''
            import sys

            class BlockPostgreSQLImports:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] in ('psycopg', 'psycopg2') or fullname.startswith('django.contrib.postgres'):
                        raise ModuleNotFoundError(fullname)

            sys.meta_path.insert(0, BlockPostgreSQLImports())
            from django.conf import settings
            settings.configure(
                INSTALLED_APPS=['django_logic'],
                DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},
                DEFAULT_AUTO_FIELD='django.db.models.BigAutoField',
                USE_TZ=True,
            )
            import django
            django.setup()
            from django.core.management import call_command
            call_command('migrate', verbosity=0)
            call_command('migrate', 'django_logic_background', '0020', verbosity=0)
        ''')
        result = subprocess.run(
            [sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[2],
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
