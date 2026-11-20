"""Worker startup checks its operating system and PostgreSQL driver."""
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from django_logic.management.commands import dl_worker
from django_logic.management.commands.dl_worker import validate_worker_database


class WorkerRequirementTests(SimpleTestCase):
    def test_unsupported_platform_stops_before_checks_or_worker_startup(self):
        for platform in ('darwin', 'win32', 'freebsd14'):
            for skip_checks in (False, True):
                with self.subTest(platform=platform, skip_checks=skip_checks), \
                        patch.object(dl_worker, 'sys', SimpleNamespace(platform=platform)), \
                        patch.object(dl_worker.Command, 'check') as check, \
                        patch.object(dl_worker.conf, 'background_execution') as mode, \
                        patch.object(dl_worker, 'validate_worker_database') as validate_database, \
                        patch.object(dl_worker.signal, 'signal') as set_signal, \
                        patch.object(dl_worker, 'run_worker') as run_worker:
                    with self.assertRaisesMessage(CommandError, 'dl_worker requires Linux.') as raised:
                        call_command('dl_worker', queues='critical', skip_checks=skip_checks)
                    self.assertIn('Linux VM or container', str(raised.exception))
                    check.assert_not_called()
                    mode.assert_not_called()
                    validate_database.assert_not_called()
                    set_signal.assert_not_called()
                    run_worker.assert_not_called()

    def test_linux_command_checks_database_and_starts_worker(self):
        with patch.object(dl_worker, 'sys', SimpleNamespace(platform='linux')), \
                patch.object(dl_worker.Command, 'check') as check, \
                patch.object(dl_worker.conf, 'background_execution', return_value=dl_worker.conf.EXECUTION_PULL), \
                patch.object(dl_worker, 'validate_worker_database') as validate_database, \
                patch.object(dl_worker.signal, 'signal') as set_signal, \
                patch.object(dl_worker, 'run_worker') as run_worker:
            call_command('dl_worker', queues='critical,fast', concurrency=2, skip_checks=False)

        check.assert_called_once()
        validate_database.assert_called_once_with()
        run_worker.assert_called_once()
        args, kwargs = run_worker.call_args
        self.assertEqual(args, (['critical', 'fast'],))
        self.assertTrue(kwargs['forever'])
        self.assertEqual(kwargs['concurrency'], 2)
        self.assertFalse(kwargs['stop_requested']())
        set_signal.assert_called_once()
        signum, handler = set_signal.call_args.args
        self.assertEqual(signum, dl_worker.signal.SIGTERM)
        handler(signum, None)
        self.assertTrue(kwargs['stop_requested']())

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
