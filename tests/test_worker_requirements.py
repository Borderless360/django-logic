"""Worker startup refuses a database driver that cannot wait for notifications."""
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from django_logic.management.commands.dl_worker import validate_worker_database


class WorkerRequirementTests(SimpleTestCase):
    def test_psycopg_three_requires_notification_timeout_support(self):
        for version in ('3.1.8', '3.1.20'):
            backend = SimpleNamespace(vendor='postgresql', Database=SimpleNamespace(__name__='psycopg', __version__=version))
            with self.subTest(version=version), patch('django_logic.management.commands.dl_worker.connection', backend):
                with self.assertRaisesMessage(CommandError, 'psycopg 3.2 or later'):
                    validate_worker_database()

    def test_current_psycopg_and_psycopg_two_are_supported(self):
        for name, version in [('psycopg', '3.2.0'), ('psycopg', '3.3.1'), ('psycopg2', '2.9.10')]:
            backend = SimpleNamespace(vendor='postgresql', Database=SimpleNamespace(__name__=name, __version__=version))
            with self.subTest(driver=name, version=version), patch('django_logic.management.commands.dl_worker.connection', backend):
                validate_worker_database()

    def test_worker_requires_postgresql(self):
        with patch('django_logic.management.commands.dl_worker.connection', SimpleNamespace(vendor='mysql')):
            with self.assertRaisesMessage(CommandError, 'requires PostgreSQL'):
                validate_worker_database()
